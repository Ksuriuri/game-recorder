local CONTROL_POLL_SECONDS = 0.01
local FLUSH_EVERY_N_SAMPLES = 30
local DEFAULT_RAW_FILE = "camera_raw_cp2077.jsonl"
local DEFAULT_NEAR_PLANE = 0.05
local DEFAULT_FAR_PLANE = 10000.0

-- CET sandboxes file I/O to this mod directory. The recorder writes this
-- control file here, and later consumes the local raw JSONL from the same dir.
local control_file = "active_session.json"
local control_missing_reported = false
local last_control_error = nil

local active_session = nil
local desired_control = nil
local desired_stop_reason = "idle"
local transition_pending = false
local rejected_session_key = nil
local control_poll_accum_seconds = 0.0
local active_camera_transform = nil

local JSON_NULL = {}

local function log(message)
    print("[CameraFrameLogger] " .. tostring(message) .. "\n")
end

local function is_finite_number(value)
    return type(value) == "number"
        and value == value
        and value ~= math.huge
        and value ~= -math.huge
end

local function round_integer(value)
    if value >= 0 then
        return math.floor(value + 0.5)
    end
    return math.ceil(value - 0.5)
end

local function format_number(value)
    if not is_finite_number(value) then
        return nil
    end
    return string.format("%.17g", value)
end

local function format_integer(value)
    if not is_finite_number(value) then
        return nil
    end
    return string.format("%.0f", round_integer(value))
end

local function json_escape(value)
    return (value:gsub('[%z\1-\31\\"]', function(character)
        local escapes = {
            ['"'] = '\\"',
            ["\\"] = "\\\\",
            ["\b"] = "\\b",
            ["\f"] = "\\f",
            ["\n"] = "\\n",
            ["\r"] = "\\r",
            ["\t"] = "\\t"
        }
        local escaped = escapes[character]
        if escaped ~= nil then
            return escaped
        end
        return string.format("\\u%04x", string.byte(character))
    end))
end

local function json_string(value)
    return '"' .. json_escape(value) .. '"'
end

local function json_number_array(values)
    local fields = {}
    for _, value in ipairs(values) do
        local formatted = format_number(value)
        if formatted == nil then
            return nil
        end
        fields[#fields + 1] = formatted
    end
    return "[" .. table.concat(fields, ",") .. "]"
end

local function normalize_path(path)
    return (path:gsub("\\", "/"))
end

local function join_path(directory, name)
    if directory:sub(-1) == "/" then
        return directory .. name
    end
    return directory .. "/" .. name
end

local function get_number(value, fallback)
    if type(value) == "number" then
        return value
    end
    if value ~= nil then
        local ok, unwrapped = pcall(function()
            return value:get()
        end)
        if ok and type(unwrapped) == "number" then
            return unwrapped
        end
    end
    return fallback
end

local function vector_components(vec)
    if vec == nil then
        return nil
    end
    local x = get_number(vec.x, nil)
    if x == nil then
        x = get_number(vec.X, nil)
    end
    local y = get_number(vec.y, nil)
    if y == nil then
        y = get_number(vec.Y, nil)
    end
    local z = get_number(vec.z, nil)
    if z == nil then
        z = get_number(vec.Z, nil)
    end
    if not is_finite_number(x) or not is_finite_number(y) or not is_finite_number(z) then
        return nil
    end
    return x, y, z
end

local function normalize3(x, y, z)
    local length = math.sqrt(x * x + y * y + z * z)
    if length <= 1e-8 then
        return nil
    end
    return x / length, y / length, z / length
end

local function cross(ax, ay, az, bx, by, bz)
    return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
end

local function dot(ax, ay, az, bx, by, bz)
    return ax * bx + ay * by + az * bz
end

local function parse_control_json(text)
    local index = 1
    local length = #text

    local function skip_whitespace()
        while index <= length and text:sub(index, index):match("%s") do
            index = index + 1
        end
    end

    local function parse_string()
        if text:sub(index, index) ~= '"' then
            return nil, "expected JSON string"
        end
        index = index + 1
        local parts = {}
        while index <= length do
            local character = text:sub(index, index)
            if character == '"' then
                index = index + 1
                return table.concat(parts)
            end
            if character == "\\" then
                index = index + 1
                if index > length then
                    return nil, "unterminated JSON escape"
                end
                local escape = text:sub(index, index)
                local simple_escapes = {
                    ['"'] = '"',
                    ["\\"] = "\\\\",
                    ["/"] = "/",
                    ["b"] = "\b",
                    ["f"] = "\f",
                    ["n"] = "\n",
                    ["r"] = "\r",
                    ["t"] = "\t"
                }
                if simple_escapes[escape] ~= nil then
                    parts[#parts + 1] = simple_escapes[escape]
                    index = index + 1
                else
                    return nil, "unsupported JSON escape"
                end
            else
                parts[#parts + 1] = character
                index = index + 1
            end
        end
        return nil, "unterminated JSON string"
    end

    local function parse_number()
        local start_index = index
        if text:sub(index, index) == "-" then
            index = index + 1
        end
        local first_digit = text:sub(index, index)
        if first_digit == "0" then
            index = index + 1
        elseif first_digit:match("[1-9]") then
            repeat
                index = index + 1
            until index > length or not text:sub(index, index):match("%d")
        else
            return nil, "invalid JSON number"
        end
        if text:sub(index, index) == "." then
            index = index + 1
            if not text:sub(index, index):match("%d") then
                return nil, "invalid JSON fraction"
            end
            repeat
                index = index + 1
            until index > length or not text:sub(index, index):match("%d")
        end
        local exponent = text:sub(index, index)
        if exponent == "e" or exponent == "E" then
            index = index + 1
            local sign = text:sub(index, index)
            if sign == "+" or sign == "-" then
                index = index + 1
            end
            if not text:sub(index, index):match("%d") then
                return nil, "invalid JSON exponent"
            end
            repeat
                index = index + 1
            until index > length or not text:sub(index, index):match("%d")
        end
        local value = tonumber(text:sub(start_index, index - 1))
        if not is_finite_number(value) then
            return nil, "JSON number is not finite"
        end
        return value
    end

    local function parse_value()
        skip_whitespace()
        local character = text:sub(index, index)
        if character == '"' then
            return parse_string()
        elseif character == "-" or character:match("%d") then
            return parse_number()
        elseif text:sub(index, index + 3) == "true" then
            index = index + 4
            return true
        elseif text:sub(index, index + 4) == "false" then
            index = index + 5
            return false
        elseif text:sub(index, index + 3) == "null" then
            index = index + 4
            return JSON_NULL
        end
        return nil, "unsupported JSON value"
    end

    skip_whitespace()
    if text:sub(index, index) ~= "{" then
        return nil, "control JSON must be an object"
    end
    index = index + 1

    local result = {}
    skip_whitespace()
    if text:sub(index, index) == "}" then
        index = index + 1
    else
        while index <= length do
            skip_whitespace()
            local key, key_error = parse_string()
            if key == nil then
                return nil, key_error
            end
            skip_whitespace()
            if text:sub(index, index) ~= ":" then
                return nil, "expected ':' after JSON key"
            end
            index = index + 1
            local value, value_error = parse_value()
            if value == nil then
                return nil, value_error
            end
            if value ~= JSON_NULL then
                result[key] = value
            end
            skip_whitespace()
            local separator = text:sub(index, index)
            if separator == "}" then
                index = index + 1
                break
            elseif separator ~= "," then
                return nil, "expected ',' or '}' in control JSON"
            end
            index = index + 1
        end
    end
    skip_whitespace()
    if index <= length then
        return nil, "trailing data after control JSON"
    end
    return result
end

local function read_control_file()
    local file, open_error = io.open(control_file, "r")
    if file == nil then
        return nil, "missing", open_error
    end
    local ok, contents = pcall(function()
        return file:read("*a")
    end)
    file:close()
    if not ok or type(contents) ~= "string" then
        return nil, "read_error", contents
    end
    local control, parse_error = parse_control_json(contents)
    if control == nil then
        return nil, "parse_error", parse_error
    end
    if type(control.status) ~= "string" then
        return nil, "parse_error", "control JSON requires string status"
    end
    return control
end

local function write_line(file, line)
    local ok, result, write_error = pcall(function()
        return file:write(line, "\n")
    end)
    if not ok then
        return false, result
    end
    if result == nil then
        return false, write_error or "unknown write error"
    end
    return true
end

local function flush_file(file)
    local ok, result, flush_error = pcall(function()
        return file:flush()
    end)
    if not ok then
        return false, result
    end
    if result == nil then
        return false, flush_error or "unknown flush error"
    end
    return true
end

local function call_method(object, method_name)
    if object == nil then
        return nil
    end
    local ok, result = pcall(function()
        local method = object[method_name]
        if method == nil then
            return nil
        end
        return method(object)
    end)
    if ok then
        return result
    end
    return nil
end

local function get_player()
    local ok, player = pcall(function()
        return Game.GetPlayer()
    end)
    if ok and player ~= nil then
        return player
    end
    return nil
end

local function get_axis_vectors(player)
    local forward = call_method(player, "GetWorldForward")
    local right = call_method(player, "GetWorldRight")
    local up = call_method(player, "GetWorldUp")

    local fx, fy, fz = vector_components(forward)
    local rx, ry, rz = vector_components(right)
    local ux, uy, uz = vector_components(up)

    fx, fy, fz = normalize3(fx or 0, fy or 0, fz or 0)
    if fx == nil then
        return nil
    end

    if rx == nil then
        rx, ry, rz = cross(fx, fy, fz, 0, 1, 0)
        rx, ry, rz = normalize3(rx, ry, rz)
    end
    if rx == nil then
        return nil
    end

    if ux == nil then
        ux, uy, uz = cross(rx, ry, rz, fx, fy, fz)
        ux, uy, uz = normalize3(ux, uy, uz)
    end
    if ux == nil then
        return nil
    end

    return fx, fy, fz, rx, ry, rz, ux, uy, uz
end

local function get_position(player)
    local pos = call_method(player, "GetWorldPosition")
    local x, y, z = vector_components(pos)
    if x ~= nil then
        return x, y, z
    end

    local transform = call_method(player, "GetWorldTransform")
    if transform ~= nil then
        local position = transform.Position or transform.position
        x, y, z = vector_components(position)
        if x ~= nil then
            return x, y, z
        end
    end
    return nil
end

local function camera_to_world_values(player)
    local px, py, pz = get_position(player)
    if px == nil then
        return nil
    end

    local fx, fy, fz, rx, ry, rz, ux, uy, uz = get_axis_vectors(player)
    if fx == nil then
        return nil
    end

    return {
        fx, fy, fz, 0.0,
        rx, ry, rz, 0.0,
        ux, uy, uz, 0.0,
        px, py, pz, 1.0
    }
end

local function create_camera_transform()
    local ok, transform = pcall(function()
        if NewObject ~= nil then
            return NewObject("Transform")
        end
        return Transform.new()
    end)
    if ok then
        return transform
    end
    return nil
end

local function get_active_camera_geometry()
    local ok_system, camera_system = pcall(function()
        return Game.GetCameraSystem()
    end)
    if not ok_system or camera_system == nil then
        return nil
    end

    if active_camera_transform == nil then
        active_camera_transform = create_camera_transform()
    end
    if active_camera_transform == nil then
        return nil
    end

    local ok_transform, valid = pcall(function()
        return camera_system:GetActiveCameraWorldTransform(active_camera_transform)
    end)
    if not ok_transform or valid ~= true then
        return nil
    end

    local px, py, pz = vector_components(active_camera_transform.position)
    local rx, ry, rz = vector_components(call_method(camera_system, "GetActiveCameraRight"))
    local ux, uy, uz = vector_components(call_method(camera_system, "GetActiveCameraUp"))
    local fx, fy, fz = vector_components(call_method(camera_system, "GetActiveCameraForward"))
    if px == nil or rx == nil or ux == nil or fx == nil then
        return nil
    end

    fx, fy, fz = normalize3(fx, fy, fz)
    ux, uy, uz = normalize3(ux, uy, uz)
    if fx == nil or ux == nil then
        return nil
    end
    rx, ry, rz = cross(fx, fy, fz, ux, uy, uz)
    rx, ry, rz = normalize3(rx, ry, rz)
    if rx == nil then
        return nil
    end
    ux, uy, uz = cross(rx, ry, rz, fx, fy, fz)
    ux, uy, uz = normalize3(ux, uy, uz)
    if ux == nil then
        return nil
    end

    local dx, dy, dz = -ux, -uy, -uz
    local camera_to_world = {
        rx, dx, fx, px,
        ry, dy, fy, py,
        rz, dz, fz, pz,
        0.0, 0.0, 0.0, 1.0
    }
    local tx = -dot(rx, ry, rz, px, py, pz)
    local ty = -dot(dx, dy, dz, px, py, pz)
    local tz = -dot(fx, fy, fz, px, py, pz)
    local world_to_camera = {
        rx, ry, rz, tx,
        dx, dy, dz, ty,
        fx, fy, fz, tz,
        0.0, 0.0, 0.0, 1.0
    }
    return {
        camera_system = camera_system,
        camera_to_world = camera_to_world,
        world_to_camera = world_to_camera,
        position_world = {px, py, pz},
        right_world = {rx, ry, rz},
        up_world = {ux, uy, uz},
        forward_world = {fx, fy, fz},
        rotation_world_to_camera = {
            rx, ry, rz,
            dx, dy, dz,
            fx, fy, fz
        },
        translation_world_to_camera = {tx, ty, tz}
    }
end

local function get_viewport_px()
    local ok, width, height = pcall(function()
        return GetDisplayResolution()
    end)
    if not ok then
        return nil, nil
    end
    width = get_number(width, nil)
    height = get_number(height, nil)
    if not is_finite_number(width) or not is_finite_number(height) then
        return nil, nil
    end
    if width <= 0 or height <= 0 then
        return nil, nil
    end
    return round_integer(width), round_integer(height)
end

local function get_mounted_vehicle(player)
    local ok, vehicle = pcall(function()
        return Game.GetMountedVehicle(player)
    end)
    if ok and vehicle ~= nil then
        return vehicle
    end
    return nil
end

local function get_camera_mode(player)
    if get_mounted_vehicle(player) ~= nil then
        return "vehicle"
    end
    local fpp = call_method(player, "GetFPPCameraComponent")
    if fpp ~= nil then
        return "fpp"
    end
    local tpp = call_method(player, "GetTPPCameraComponent")
    if tpp ~= nil then
        return "tpp"
    end
    return "player"
end

local function get_fov_degrees(camera_system, player, camera_mode, viewport_width, viewport_height)
    local fov_axis = "horizontal"
    local fov_source = "graphics_settings"
    local fov_value = nil

    local active_fov = get_number(call_method(camera_system, "GetActiveCameraFOV"), nil)
    if active_fov ~= nil and active_fov > 1.0 and active_fov < 179.0 then
        fov_value = active_fov
        fov_axis = "vertical"
        fov_source = "GetActiveCameraFOV"
    end

    if fov_value == nil then
        local ok, settings_value = pcall(function()
            local settings = Game.GetSettingsSystem()
            local var = settings:GetVar("/graphics/basic/FieldOfView")
            return var:GetValue()
        end)
        if ok and is_finite_number(settings_value) then
            fov_value = settings_value
        end
    end

    if fov_source ~= "GetActiveCameraFOV" and camera_mode == "vehicle" then
        fov_axis = "vertical"
        local vehicle = get_mounted_vehicle(player)
        local camera = vehicle and call_method(vehicle, "GetCameraComponent") or nil
        local internal_fov = camera and get_number(call_method(camera, "GetFOV"), nil) or nil
        if internal_fov ~= nil then
            fov_value = internal_fov
            fov_source = "vehicle_camera_internal"
        end
    elseif fov_source ~= "GetActiveCameraFOV" then
        local fpp = call_method(player, "GetFPPCameraComponent")
        local internal_fov = fpp and get_number(call_method(fpp, "GetFOV"), nil) or nil
        if internal_fov ~= nil and camera_mode == "fpp" then
            fov_value = internal_fov
            fov_source = "fpp_camera_internal"
            fov_axis = "internal"
        end
    end

    if fov_value == nil then
        fov_value = 80.0
        fov_source = "default"
    end

    local hfov_deg = fov_value
    local vfov_deg = fov_value
    if viewport_width ~= nil and viewport_height ~= nil and viewport_height > 0 then
        local aspect = viewport_width / viewport_height
        if fov_axis == "horizontal" then
            local hfov_rad = math.rad(hfov_deg)
            vfov_deg = math.deg(2.0 * math.atan(math.tan(hfov_rad * 0.5) / aspect))
        elseif fov_axis == "vertical" then
            local vfov_rad = math.rad(vfov_deg)
            hfov_deg = math.deg(2.0 * math.atan(math.tan(vfov_rad * 0.5) * aspect))
        else
            local vfov_rad = math.rad(vfov_deg)
            hfov_deg = math.deg(2.0 * math.atan(math.tan(vfov_rad * 0.5) * aspect))
            fov_axis = "vertical"
        end
    end

    return hfov_deg, vfov_deg, fov_axis, fov_source
end

local function build_intrinsic(width, height, hfov_deg, vfov_deg)
    local hfov_rad = math.rad(hfov_deg)
    local vfov_rad = math.rad(vfov_deg)
    local fx = width / (2.0 * math.tan(hfov_rad * 0.5))
    local fy = height / (2.0 * math.tan(vfov_rad * 0.5))
    return {
        fx = fx,
        fy = fy,
        cx = width * 0.5,
        cy = height * 0.5,
        width = width,
        height = height
    }
end

local function build_world_to_clip(width, height, hfov_deg, vfov_deg, near_plane, far_plane)
    local hfov_rad = math.rad(hfov_deg)
    local vfov_rad = math.rad(vfov_deg)
    local x_scale = 1.0 / math.tan(hfov_rad * 0.5)
    local y_scale = 1.0 / math.tan(vfov_rad * 0.5)
    local z_range = far_plane - near_plane
    if z_range <= 0 then
        return nil
    end
    local a = far_plane / z_range
    local b = (-far_plane * near_plane) / z_range
    return {
        x_scale, 0.0, 0.0, 0.0,
        0.0, y_scale, 0.0, 0.0,
        0.0, 0.0, a, 1.0,
        0.0, 0.0, b, 0.0
    }
end

local function build_world_to_pixel(intrinsic, world_to_camera)
    local fx, fy = intrinsic.fx, intrinsic.fy
    local cx, cy = intrinsic.cx, intrinsic.cy
    local t = world_to_camera
    return {
        fx * t[1] + cx * t[9],
        fx * t[2] + cx * t[10],
        fx * t[3] + cx * t[11],
        fx * t[4] + cx * t[12],
        fy * t[5] + cy * t[9],
        fy * t[6] + cy * t[10],
        fy * t[7] + cy * t[11],
        fy * t[8] + cy * t[12],
        t[9], t[10], t[11], t[12]
    }
end

local function close_session(reason)
    local session = active_session
    if session == nil then
        return
    end
    active_session = nil

    local end_unix_ms = session.last_t_unix_ms or session.anchor_unix_ms
    local footer = '{"type":"footer","end_unix_ms":'
        .. format_integer(end_unix_ms)
        .. ',"sample_count":'
        .. tostring(session.sample_count)
        .. ',"reason":'
        .. json_string(reason)
        .. "}"
    write_line(session.file, footer)
    flush_file(session.file)
    pcall(function()
        session.file:close()
    end)
    log(
        "Session closed: "
            .. session.session_id
            .. " ("
            .. reason
            .. ", "
            .. tostring(session.sample_count)
            .. " samples)"
    )
end

local function sample_camera(session, delta_time)
    if active_session ~= session then
        return
    end

    session.elapsed_seconds = session.elapsed_seconds + math.max(0.0, delta_time or 0.0)
    if session.elapsed_seconds + 0.0000001 < session.next_sample_seconds then
        return
    end

    local periods = math.floor(
        (session.elapsed_seconds - session.next_sample_seconds)
            / session.sample_period_seconds
    )
    if periods < 0 then
        periods = 0
    end
    session.next_sample_seconds =
        session.next_sample_seconds
        + (periods + 1) * session.sample_period_seconds

    local geometry = get_active_camera_geometry()
    if geometry == nil then
        return
    end
    local player = get_player()

    local viewport_width, viewport_height = get_viewport_px()
    if viewport_width == nil or viewport_height == nil then
        return
    end

    local camera_mode = get_camera_mode(player)
    local hfov_deg, vfov_deg, fov_axis, fov_source = get_fov_degrees(
        geometry.camera_system,
        player,
        camera_mode,
        viewport_width,
        viewport_height
    )
    local intrinsic = build_intrinsic(viewport_width, viewport_height, hfov_deg, vfov_deg)
    local world_to_pixel = build_world_to_pixel(intrinsic, geometry.world_to_camera)

    local t_unix_ms = round_integer(
        session.anchor_unix_ms + session.elapsed_seconds * 1000.0
    )
    local camera_to_world = json_number_array(geometry.camera_to_world)
    local world_to_camera = json_number_array(geometry.world_to_camera)
    local world_to_pixel_json = json_number_array(world_to_pixel)
    if camera_to_world == nil or world_to_camera == nil or world_to_pixel_json == nil then
        return
    end

    local line = '{"type":"sample","t_unix_ms":'
        .. format_integer(t_unix_ms)
        .. ',"camera_to_world":'
        .. camera_to_world
        .. ',"world_to_camera":'
        .. world_to_camera
        .. ',"camera_position_world":'
        .. json_number_array(geometry.position_world)
        .. ',"camera_right_world":'
        .. json_number_array(geometry.right_world)
        .. ',"camera_up_world":'
        .. json_number_array(geometry.up_world)
        .. ',"camera_forward_world":'
        .. json_number_array(geometry.forward_world)
        .. ',"rotation_world_to_camera":'
        .. json_number_array(geometry.rotation_world_to_camera)
        .. ',"translation_world_to_camera":'
        .. json_number_array(geometry.translation_world_to_camera)
        .. ',"fov_horizontal_deg":'
        .. format_number(hfov_deg)
        .. ',"fov_vertical_deg":'
        .. format_number(vfov_deg)
        .. ',"fov_axis":'
        .. json_string(fov_axis)
        .. ',"fov_source":'
        .. json_string(fov_source)
        .. ',"camera_mode":'
        .. json_string(camera_mode)
        .. ',"viewport_px":['
        .. format_integer(viewport_width)
        .. ","
        .. format_integer(viewport_height)
        .. '],"intrinsic":{"fx":'
        .. format_number(intrinsic.fx)
        .. ',"fy":'
        .. format_number(intrinsic.fy)
        .. ',"cx":'
        .. format_number(intrinsic.cx)
        .. ',"cy":'
        .. format_number(intrinsic.cy)
        .. ',"width":'
        .. format_integer(intrinsic.width)
        .. ',"height":'
        .. format_integer(intrinsic.height)
        .. '},"near_plane":'
        .. format_number(session.near_plane)
        .. ',"far_plane":'
        .. format_number(session.far_plane)

    line = line .. ',"world_to_pixel":' .. world_to_pixel_json
    line = line .. "}"

    local write_ok, write_error = write_line(session.file, line)
    if not write_ok then
        log("Failed to write camera sample: " .. tostring(write_error))
        close_session("write_error")
        return
    end

    session.last_t_unix_ms = t_unix_ms
    session.sample_count = session.sample_count + 1
    if session.sample_count % FLUSH_EVERY_N_SAMPLES == 0 then
        local flush_ok, flush_error = flush_file(session.file)
        if not flush_ok then
            log("Failed to flush camera output: " .. tostring(flush_error))
            close_session("flush_error")
        end
    end
end

local function validate_recording_control(control)
    if type(control.session_id) ~= "string" or control.session_id == "" then
        return nil, "recording control requires session_id"
    end
    if type(control.session_dir) ~= "string" or control.session_dir == "" then
        return nil, "recording control requires session_dir"
    end
    if control.raw_file == nil then
        control.raw_file = DEFAULT_RAW_FILE
    end
    if type(control.raw_file) ~= "string" or control.raw_file == "" then
        return nil, "raw_file must be a relative file name"
    end
    if control.raw_file:match("^[/\\]") or control.raw_file:match("^%a:[/\\]") then
        return nil, "raw_file must be relative to session_dir"
    end
    if not is_finite_number(control.sample_hz) or control.sample_hz <= 0 then
        control.sample_hz = 30.0
    end
    if not is_finite_number(control.start_epoch_ms) then
        return nil, "recording control requires start_epoch_ms"
    end

    control.session_dir = normalize_path(control.session_dir)
    control.raw_file = normalize_path(control.raw_file)
    control._session_key = table.concat({
        control.session_id,
        control.session_dir,
        control.raw_file,
        format_number(control.sample_hz),
        format_number(control.start_epoch_ms),
        format_number(control.updated_at_ms or control.start_epoch_ms)
    }, "\0")
    return control
end

local function start_session(control)
    local output_path = control.raw_file
    local output_file, open_error = io.open(output_path, "w")
    if output_file == nil then
        log("Session rejected: cannot open " .. output_path .. ": " .. tostring(open_error))
        return false
    end

    -- The recorder publishes control only after its capture threads are ready,
    -- which can be over a second after start_epoch_ms. Anchor sample timestamps
    -- to the publish time so they align with frame_timestamps.jsonl.
    local anchor_unix_ms = round_integer(
        is_finite_number(control.updated_at_ms)
            and control.updated_at_ms
            or control.start_epoch_ms
    )
    local header = '{"type":"header","schema":"cp2077_camera_v3"'
        .. ',"start_unix_ms":'
        .. format_integer(anchor_unix_ms)
        .. ',"sample_hz":'
        .. format_number(control.sample_hz)
        .. ',"session_id":'
        .. json_string(control.session_id)
        .. ',"world_units":"meters"'
        .. ',"camera_to_world_translation_units":"meters"'
        .. ',"matrix_layout":"row_major"'
        .. ',"matrix_vector_convention":"column_vector"'
        .. ',"world_axes":"x_game_y_game_z_up"'
        .. ',"camera_axes":"x_right_y_down_z_forward"'
        .. ',"camera_to_world_source":"GetActiveCameraWorldTransform_and_active_camera_axes"'
        .. ',"world_to_pixel_source":"intrinsic_times_world_to_camera"'
        .. ',"fov_axis_default":"vertical_from_GetActiveCameraFOV"'
        .. ',"projection_source":"K_times_OpenCV_world_to_camera"'
        .. ',"sample_policy":"active_render_camera_available"'
        .. ',"clock":"recorder_publish_unix_plus_game_delta_seconds"}'

    local header_ok, header_error = write_line(output_file, header)
    if not header_ok then
        pcall(function()
            output_file:close()
        end)
        log("Session rejected: cannot write header: " .. tostring(header_error))
        return false
    end
    flush_file(output_file)

    local session = {
        key = control._session_key,
        session_id = control.session_id,
        file = output_file,
        output_path = output_path,
        sample_hz = control.sample_hz,
        sample_period_seconds = 1.0 / control.sample_hz,
        anchor_unix_ms = anchor_unix_ms,
        elapsed_seconds = 0.0,
        next_sample_seconds = 0.0,
        near_plane = DEFAULT_NEAR_PLANE,
        far_plane = DEFAULT_FAR_PLANE,
        last_t_unix_ms = nil,
        sample_count = 0
    }
    active_session = session
    rejected_session_key = nil
    log("Session started: " .. control.session_id .. " -> " .. output_path)
    return true
end

local function reconcile_desired_state()
    local control = desired_control
    if control == nil then
        rejected_session_key = nil
        if active_session ~= nil then
            close_session(desired_stop_reason)
        end
        return
    end

    if active_session ~= nil and active_session.key ~= control._session_key then
        close_session("session_switched")
    end

    if active_session == nil and rejected_session_key ~= control._session_key then
        if not start_session(control) then
            rejected_session_key = control._session_key
        end
    end
end

local function request_reconcile(control, stop_reason)
    desired_control = control
    desired_stop_reason = stop_reason or "idle"
    if transition_pending then
        return
    end
    if control == nil and active_session == nil then
        rejected_session_key = nil
        return
    end
    if control ~= nil then
        if active_session ~= nil and active_session.key == control._session_key then
            return
        end
        if active_session == nil and rejected_session_key == control._session_key then
            return
        end
    end
    transition_pending = true
    reconcile_desired_state()
    transition_pending = false
end

local function poll_control()
    local control, error_kind, detail = read_control_file()
    if control == nil then
        if error_kind == "missing" then
            if not control_missing_reported then
                log("Control file not found: " .. control_file .. "; waiting")
                control_missing_reported = true
            end
            last_control_error = nil
            request_reconcile(nil, "control_removed")
        else
            local message = error_kind .. ": " .. tostring(detail)
            if message ~= last_control_error then
                log("Invalid control file: " .. message)
                last_control_error = message
            end
            request_reconcile(nil, "control_invalid")
        end
        return
    end

    control_missing_reported = false
    last_control_error = nil
    if control.status ~= "recording" then
        request_reconcile(nil, control.status == "idle" and "idle" or "status_not_recording")
        return
    end

    local valid_control, validation_error = validate_recording_control(control)
    if valid_control == nil then
        if validation_error ~= last_control_error then
            log("Invalid recording control: " .. validation_error)
            last_control_error = validation_error
        end
        request_reconcile(nil, "control_invalid")
        return
    end
    request_reconcile(valid_control)
end

registerForEvent("onInit", function()
    log("Loaded. Using sandbox-local control file: " .. control_file)
end)

registerForEvent("onUpdate", function(delta_time)
    control_poll_accum_seconds =
        control_poll_accum_seconds + (delta_time or 0.0)
    if control_poll_accum_seconds >= CONTROL_POLL_SECONDS then
        control_poll_accum_seconds = 0.0
        local ok, poll_error = pcall(poll_control)
        if not ok then
            log("Control polling failed: " .. tostring(poll_error))
        end
    end

    local session = active_session
    if session ~= nil then
        local ok, sample_error = pcall(sample_camera, session, delta_time)
        if not ok then
            log("Camera sampling failed: " .. tostring(sample_error))
            if active_session == session then
                close_session("sampling_error")
            end
        end
    end
end)

registerForEvent("onShutdown", function()
    if active_session ~= nil then
        close_session("shutdown")
    end
end)
