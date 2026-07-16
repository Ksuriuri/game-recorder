local CONTROL_POLL_MS = 10
local FLUSH_EVERY_N_SAMPLES = 30
local DEFAULT_RAW_FILE = "camera_raw_wukong.jsonl"

local win64_dir = nil
local config_path = nil
local control_file = nil
local config_retry_ticks = 0
local last_config_error = nil
local control_missing_reported = false
local last_control_error = nil

local cached_camera_manager = nil
local cached_engine_func_lib = nil
local cached_player_controller = nil
local active_session = nil
local desired_control = nil
local desired_stop_reason = "idle"
local transition_pending = false
local rejected_session_key = nil
local retryable_session_key = nil
local retryable_session_poll_count = 0
local CLOCK_RETRY_POLLS = 10

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

local function codepoint_to_utf8(codepoint)
    if codepoint <= 0x7F then
        return string.char(codepoint)
    elseif codepoint <= 0x7FF then
        return string.char(
            0xC0 + math.floor(codepoint / 0x40),
            0x80 + (codepoint % 0x40)
        )
    elseif codepoint <= 0xFFFF then
        return string.char(
            0xE0 + math.floor(codepoint / 0x1000),
            0x80 + (math.floor(codepoint / 0x40) % 0x40),
            0x80 + (codepoint % 0x40)
        )
    elseif codepoint <= 0x10FFFF then
        return string.char(
            0xF0 + math.floor(codepoint / 0x40000),
            0x80 + (math.floor(codepoint / 0x1000) % 0x40),
            0x80 + (math.floor(codepoint / 0x40) % 0x40),
            0x80 + (codepoint % 0x40)
        )
    end
    return nil
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
                    ["\\"] = "\\",
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
                elseif escape == "u" then
                    local hex = text:sub(index + 1, index + 4)
                    if #hex ~= 4 or not hex:match("^%x%x%x%x$") then
                        return nil, "invalid JSON unicode escape"
                    end
                    local codepoint = tonumber(hex, 16)
                    index = index + 5

                    if codepoint >= 0xD800 and codepoint <= 0xDBFF then
                        if text:sub(index, index + 1) ~= "\\u" then
                            return nil, "missing low surrogate"
                        end
                        local low_hex = text:sub(index + 2, index + 5)
                        if #low_hex ~= 4 or not low_hex:match("^%x%x%x%x$") then
                            return nil, "invalid low surrogate"
                        end
                        local low = tonumber(low_hex, 16)
                        if low < 0xDC00 or low > 0xDFFF then
                            return nil, "invalid low surrogate"
                        end
                        codepoint = 0x10000 + (codepoint - 0xD800) * 0x400 + (low - 0xDC00)
                        index = index + 6
                    elseif codepoint >= 0xDC00 and codepoint <= 0xDFFF then
                        return nil, "unexpected low surrogate"
                    end

                    local encoded = codepoint_to_utf8(codepoint)
                    if encoded == nil then
                        return nil, "unicode codepoint out of range"
                    end
                    parts[#parts + 1] = encoded
                else
                    return nil, "unsupported JSON escape"
                end
            else
                if string.byte(character) < 0x20 then
                    return nil, "unescaped JSON control character"
                end
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

local function is_valid(obj)
    if obj == nil then
        return false
    end
    local ok, valid = pcall(function()
        return obj:IsValid()
    end)
    return ok and valid
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

local function win64_from_game_directory(game_directory)
    if type(game_directory) ~= "table"
        or type(game_directory.Binaries) ~= "table"
        or type(game_directory.Binaries.Win64) ~= "table" then
        return nil
    end

    local absolute_path = game_directory.Binaries.Win64.__absolute_path
    if type(absolute_path) == "string" and absolute_path ~= "" then
        return normalize_path(absolute_path)
    end
    return nil
end

local function discover_win64_directory()
    local ok, directories = pcall(IterateGameDirectories)
    if not ok or type(directories) ~= "table" then
        return nil, "IterateGameDirectories failed"
    end

    local preferred = win64_from_game_directory(directories.b1)
    if preferred ~= nil then
        return preferred
    end

    for name, game_directory in pairs(directories) do
        if name ~= "Engine" then
            local candidate = win64_from_game_directory(game_directory)
            if candidate ~= nil then
                return candidate
            end
        end
    end
    return nil, "no game Binaries/Win64 directory was found"
end

local function report_config_error(message)
    if message ~= last_config_error then
        log("Configuration unavailable: " .. message .. "; waiting for config.lua")
        last_config_error = message
    end
end

local function load_config()
    if win64_dir == nil then
        local discovery_error
        win64_dir, discovery_error = discover_win64_directory()
        if win64_dir == nil then
            report_config_error(discovery_error)
            return false
        end
        config_path = join_path(
            win64_dir,
            "ue4ss/Mods/CameraFrameLogger/config.lua"
        )
    end

    local probe = io.open(config_path, "r")
    if probe == nil then
        report_config_error("not found at " .. config_path)
        return false
    end
    probe:close()

    local ok, config = pcall(dofile, config_path)
    if not ok then
        report_config_error("failed to load " .. config_path .. ": " .. tostring(config))
        return false
    end
    if type(config) ~= "table"
        or type(config.control_file) ~= "string"
        or config.control_file == "" then
        report_config_error("config.lua must return { control_file = \"...\" }")
        return false
    end

    control_file = normalize_path(config.control_file)
    last_config_error = nil
    log("Using control file: " .. control_file)
    return true
end

local function get_capture_clock_seconds()
    -- On Windows, Lua os.clock() is backed by the CRT monotonic process clock.
    -- It avoids UE4SS UObject/UFunction bindings, which are unavailable in this
    -- packaged build. A 10 ms control poll bounds start-anchor skew.
    local ok, value = pcall(os.clock)
    if not ok or not is_finite_number(value) then
        return nil, "Lua os.clock() was unavailable"
    end
    return value
end

local function get_camera_manager()
    if is_valid(cached_camera_manager) then
        return cached_camera_manager
    end

    local class_names = {
        "BP_B1PlayerCameraManager_C",
        "BGP_PlayerCameraManagerCS"
    }
    for _, class_name in ipairs(class_names) do
        local ok, object = pcall(FindFirstOf, class_name)
        if ok and is_valid(object) then
            cached_camera_manager = object
            log("Camera manager found: " .. class_name)
            return object
        end
    end

    cached_camera_manager = nil
    return nil
end

local MATRIX_PLANES = { "XPlane", "YPlane", "ZPlane", "WPlane" }
local MATRIX_COMPONENTS = { "X", "Y", "Z", "W" }

local function matrix_values(matrix)
    if matrix == nil then
        return nil
    end

    local values = {}
    for _, plane_name in ipairs(MATRIX_PLANES) do
        local plane_ok, plane = pcall(function()
            return matrix[plane_name]
        end)
        if not plane_ok or plane == nil then
            return nil
        end
        for _, component_name in ipairs(MATRIX_COMPONENTS) do
            local component_ok, component = pcall(function()
                return plane[component_name]
            end)
            local value = component_ok and get_number(component, nil) or nil
            if not is_finite_number(value) then
                return nil
            end
            values[#values + 1] = value
        end
    end
    return values
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

local function get_engine_func_lib()
    if is_valid(cached_engine_func_lib) then
        return cached_engine_func_lib
    end

    local ok, object = pcall(
        StaticFindObject,
        "/Script/UnrealExtent.Default__GSE_EngineFuncLib"
    )
    if ok and is_valid(object) then
        cached_engine_func_lib = object
        log("Wukong GSE_EngineFuncLib found")
        return object
    end
    cached_engine_func_lib = nil
    return nil
end

local function get_player_controller(camera_manager)
    if is_valid(cached_player_controller) then
        return cached_player_controller
    end

    local class_names = {
        "BP_B1PlayerController_C",
        "BGP_PlayerControllerB1",
        "BGP_PlayerControllerCS",
        "BGPPlayerController"
    }
    for _, class_name in ipairs(class_names) do
        local ok, object = pcall(FindFirstOf, class_name)
        if ok and is_valid(object) then
            cached_player_controller = object
            log("Player controller found: " .. class_name)
            return object
        end
    end

    local ok, object = pcall(function()
        return camera_manager:GetOwningPlayerController()
    end)
    if ok and is_valid(object) then
        cached_player_controller = object
        log("Player controller found from camera manager")
        return object
    end
    cached_player_controller = nil
    return nil
end

local function get_viewport_size(camera_manager)
    local controller = get_player_controller(camera_manager)
    if controller == nil then
        return nil, nil
    end

    local size_x = {}
    local size_y = {}
    local ok = pcall(function()
        controller:GetViewportSize(size_x, size_y)
    end)
    if not ok then
        return nil, nil
    end

    local width = get_number(size_x.SizeX, nil)
    local height = get_number(size_y.SizeY, nil)
    if not is_finite_number(width)
        or not is_finite_number(height)
        or width <= 0
        or height <= 0 then
        return nil, nil
    end
    return round_integer(width), round_integer(height)
end

local function get_world_to_clip(camera_manager)
    local engine_func_lib = get_engine_func_lib()
    local controller = get_player_controller(camera_manager)
    if engine_func_lib == nil or controller == nil then
        return nil
    end

    local ok, matrix = pcall(function()
        return engine_func_lib:GetPlayerViewProjectionMatrix(controller)
    end)
    if not ok then
        return nil
    end
    return matrix_values(matrix)
end

local function camera_to_world_values(camera)
    -- UE FRotator convention: local X is forward, Y is right, Z is up.
    -- The returned row-major matrix maps UE camera-local row vectors to world.
    local pitch = math.rad(camera.pitch)
    local yaw = math.rad(camera.yaw)
    local roll = math.rad(camera.roll)
    local sp, cp = math.sin(pitch), math.cos(pitch)
    local sy, cy = math.sin(yaw), math.cos(yaw)
    local sr, cr = math.sin(roll), math.cos(roll)

    local forward = { cp * cy, cp * sy, sp }
    local right = {
        sr * sp * cy - cr * sy,
        sr * sp * sy + cr * cy,
        -sr * cp
    }
    local up = {
        -(cr * sp * cy + sr * sy),
        cy * sr - cr * sp * sy,
        cr * cp
    }
    return {
        forward[1], forward[2], forward[3], 0.0,
        right[1], right[2], right[3], 0.0,
        up[1], up[2], up[3], 0.0,
        camera.x / 100.0, camera.y / 100.0, camera.z / 100.0, 1.0
    }
end

local function read_camera_cache(camera_manager)
    local ok, camera = pcall(function()
        local cache = camera_manager.CameraCachePrivate
        local pov = cache and cache.POV or nil
        if cache == nil or pov == nil then
            return nil
        end

        local location = pov.Location
        local rotation = pov.Rotation
        if location == nil or rotation == nil then
            return nil
        end

        return {
            timestamp = get_number(cache.Timestamp, nil),
            x = get_number(location.X, nil),
            y = get_number(location.Y, nil),
            z = get_number(location.Z, nil),
            pitch = get_number(rotation.Pitch, nil),
            roll = get_number(rotation.Roll, nil),
            yaw = get_number(rotation.Yaw, nil),
            projection_mode = get_number(pov.ProjectionMode, 0)
        }
    end)
    if not ok or camera == nil then
        return nil
    end

    for _, field in ipairs({
        "timestamp", "x", "y", "z", "pitch", "roll", "yaw", "projection_mode"
    }) do
        if not is_finite_number(camera[field]) then
            return nil
        end
    end
    return camera
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

local function close_session(reason)
    local session = active_session
    if session == nil then
        return
    end

    active_session = nil

    local end_unix_ms = session.last_t_unix_ms or session.anchor_unix_ms
    local platform_now = get_capture_clock_seconds()
    if is_finite_number(platform_now) then
        end_unix_ms = round_integer(
            session.anchor_unix_ms
                + (platform_now - session.anchor_platform_seconds) * 1000.0
        )
    end

    local footer = '{"type":"footer","end_unix_ms":'
        .. format_integer(end_unix_ms)
        .. ',"sample_count":'
        .. tostring(session.sample_count)
        .. ',"reason":'
        .. json_string(reason)
        .. "}"
    local footer_ok, footer_error = write_line(session.file, footer)
    if not footer_ok then
        log("Failed to write footer: " .. tostring(footer_error))
    end
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

local function sample_camera(session)
    if active_session ~= session then
        return
    end

    local platform_now, clock_error = get_capture_clock_seconds()
    if platform_now == nil then
        log("Stopping session because platform time failed: " .. clock_error)
        close_session("clock_error")
        return
    end

    if platform_now + 0.000000001 < session.next_sample_platform_seconds then
        return
    end

    local elapsed_periods = math.floor(
        (platform_now - session.next_sample_platform_seconds)
            / session.sample_period_seconds
    )
    if elapsed_periods < 0 then
        elapsed_periods = 0
    end
    session.next_sample_platform_seconds =
        session.next_sample_platform_seconds
        + (elapsed_periods + 1) * session.sample_period_seconds

    local camera_manager = get_camera_manager()
    if camera_manager == nil then
        return
    end

    local camera = read_camera_cache(camera_manager)
    if camera == nil or camera.timestamp == session.last_camera_timestamp then
        return
    end

    local t_unix_ms = round_integer(
        session.anchor_unix_ms
            + (platform_now - session.anchor_platform_seconds) * 1000.0
    )
    local t_formatted = format_integer(t_unix_ms)
    local camera_to_world = json_number_array(camera_to_world_values(camera))
    if t_formatted == nil or camera_to_world == nil then
        return
    end

    local line = '{"type":"sample","t_unix_ms":'
        .. t_formatted
        .. ',"camera_to_world":'
        .. camera_to_world
        .. ',"projection_mode":'
        .. format_integer(camera.projection_mode)

    local world_to_clip = get_world_to_clip(camera_manager)
    if world_to_clip ~= nil then
        local world_to_clip_json = json_number_array(world_to_clip)
        if world_to_clip_json ~= nil then
            line = line .. ',"world_to_clip":' .. world_to_clip_json
        else
            line = line .. ',"projection_status":"unavailable"'
        end
    else
        line = line .. ',"projection_status":"unavailable"'
    end

    local viewport_width, viewport_height = get_viewport_size(camera_manager)
    if viewport_width ~= nil and viewport_height ~= nil then
        line = line
            .. ',"viewport_px":['
            .. format_integer(viewport_width)
            .. ","
            .. format_integer(viewport_height)
            .. "]"
    end
    line = line .. "}"

    local write_ok, write_error = write_line(session.file, line)
    if not write_ok then
        log("Failed to write camera sample: " .. tostring(write_error))
        close_session("write_error")
        return
    end

    session.last_camera_timestamp = camera.timestamp
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

local function start_sampling_loop(session)
    local loop_delay_ms = math.floor(1000.0 / session.sample_hz)
    if loop_delay_ms < 1 then
        loop_delay_ms = 1
    elseif loop_delay_ms > CONTROL_POLL_MS then
        loop_delay_ms = CONTROL_POLL_MS
    end

    LoopAsync(loop_delay_ms, function()
        if active_session ~= session then
            return true
        end

        local scheduled, schedule_error = pcall(function()
            ExecuteInGameThread(function()
                local ok, sample_error = pcall(sample_camera, session)
                if not ok then
                    log("Camera sampling failed: " .. tostring(sample_error))
                    if active_session == session then
                        close_session("sampling_error")
                    end
                end
            end)
        end)
        if not scheduled then
            log("Could not schedule camera sample: " .. tostring(schedule_error))
            return true
        end
        return false
    end)
end

local function start_session(control)
    local platform_now, clock_error = get_capture_clock_seconds()
    if platform_now == nil then
        log("Session start delayed: " .. clock_error .. "; will retry")
        return false, true
    end

    local anchor_platform_seconds = platform_now

    local output_path = join_path(control.session_dir, control.raw_file)
    local output_file, open_error = io.open(output_path, "w")
    if output_file == nil then
        log("Session rejected: cannot open " .. output_path .. ": " .. tostring(open_error))
        return false
    end

    local anchor_unix_ms = round_integer(control.qpc_anchor_unix_ms)
    local header = '{"type":"header","schema":"wukong_camera_v2"'
        .. ',"start_unix_ms":'
        .. format_integer(anchor_unix_ms)
        .. ',"sample_hz":'
        .. format_number(control.sample_hz)
        .. ',"session_id":'
        .. json_string(control.session_id)
        .. ',"camera_to_world_translation_units":"meters"'
        .. ',"matrix_layout":"row_major"'
        .. ',"matrix_vector_convention":"row_vector"'
        .. ',"world_axes":"x_forward_y_right_z_up"'
        .. ',"camera_axes":"x_forward_y_right_z_up"'
        .. ',"camera_to_world_source":"camera_cache_pov_rotation"'
        .. ',"world_to_clip_source":"gse_engine_func_lib"'
        .. ',"world_to_clip_input_units":"centimeters"'
        .. ',"clock":"lua_os_clock_anchored_to_recorder_qpc"}'
    local header_ok, header_error = write_line(output_file, header)
    if not header_ok then
        pcall(function()
            output_file:close()
        end)
        log("Session rejected: cannot write header: " .. tostring(header_error))
        return false
    end
    local flush_ok, flush_error = flush_file(output_file)
    if not flush_ok then
        pcall(function()
            output_file:close()
        end)
        log("Session rejected: cannot flush header: " .. tostring(flush_error))
        return false
    end

    local session = {
        key = control._session_key,
        session_id = control.session_id,
        file = output_file,
        output_path = output_path,
        sample_hz = control.sample_hz,
        sample_period_seconds = 1.0 / control.sample_hz,
        anchor_platform_seconds = anchor_platform_seconds,
        anchor_unix_ms = anchor_unix_ms,
        next_sample_platform_seconds = platform_now,
        last_camera_timestamp = nil,
        last_t_unix_ms = nil,
        sample_count = 0
    }
    active_session = session
    rejected_session_key = nil
    start_sampling_loop(session)
    log(
        "Session started: "
            .. control.session_id
            .. " -> "
            .. output_path
            .. " (clock: Lua os.clock)"
    )
    return true
end

local function reconcile_desired_state()
    local control = desired_control
    if control == nil then
        rejected_session_key = nil
        retryable_session_key = nil
        retryable_session_poll_count = 0
        if active_session ~= nil then
            close_session(desired_stop_reason)
        end
        return
    end

    if active_session ~= nil and active_session.key ~= control._session_key then
        close_session("session_switched")
    end

    if active_session == nil then
        if rejected_session_key == control._session_key then
            return
        end
        if retryable_session_key == control._session_key then
            retryable_session_poll_count = retryable_session_poll_count + 1
            if retryable_session_poll_count < CLOCK_RETRY_POLLS then
                return
            end
            retryable_session_key = nil
            retryable_session_poll_count = 0
        end
        local started, retryable = start_session(control)
        if not started and retryable then
            retryable_session_key = control._session_key
            retryable_session_poll_count = 0
        elseif not started then
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
    local scheduled, schedule_error = pcall(function()
        ExecuteInGameThread(function()
            local ok, reconcile_error = pcall(reconcile_desired_state)
            transition_pending = false
            if not ok then
                log("Session transition failed: " .. tostring(reconcile_error))
            end
        end)
    end)
    if not scheduled then
        transition_pending = false
        log("Could not schedule session transition: " .. tostring(schedule_error))
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
        return nil, "sample_hz must be a positive finite number"
    end
    if not is_finite_number(control.qpc_anchor_seconds) then
        return nil, "recording control requires qpc_anchor_seconds"
    end
    if not is_finite_number(control.qpc_anchor_unix_ms) then
        return nil, "recording control requires qpc_anchor_unix_ms"
    end

    control.session_dir = normalize_path(control.session_dir)
    control.raw_file = normalize_path(control.raw_file)
    control._session_key = table.concat({
        control.session_id,
        control.session_dir,
        control.raw_file,
        format_number(control.sample_hz),
        format_number(control.qpc_anchor_seconds),
        format_number(control.qpc_anchor_unix_ms)
    }, "\0")
    return control
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

local function poll_control()
    if control_file == nil then
        config_retry_ticks = config_retry_ticks + 1
        if config_retry_ticks == 1 or config_retry_ticks >= 10 then
            config_retry_ticks = 0
            load_config()
        end
        if control_file == nil then
            request_reconcile(nil, "control_removed")
            return
        end
    end

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

LoopAsync(CONTROL_POLL_MS, function()
    local ok, poll_error = pcall(poll_control)
    if not ok then
        log("Control polling failed: " .. tostring(poll_error))
    end
    return false
end)

log("Loaded. Polling every 100 ms for game-recorder session control.")
