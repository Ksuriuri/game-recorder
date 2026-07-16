#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <limits>
#include <locale>
#include <sstream>
#include <string>
#include <utility>

#include <main.h>
#include <nativeCaller.h>
#include <types.h>

namespace
{
namespace fs = std::filesystem;

constexpr wchar_t kConfigName[] = L"rdr2_camera.config.json";
constexpr std::uint64_t kFileTimeUnixEpoch = 116444736000000000ULL;

HMODULE g_module = nullptr;

struct PluginConfig
{
    fs::path control_file;
    std::uint64_t poll_interval_ms = 100;
    std::uint64_t flush_every_samples = 30;
};

struct ControlState
{
    std::string status;
    std::string session_id;
    std::string session_dir;
    std::string raw_file = "camera_raw_rdr2.jsonl";
    std::int64_t start_epoch_ms = 0;
    double sample_hz = 30.0;
    bool has_start_epoch_ms = false;
};

struct JsonFields
{
    std::string status;
    std::string session_id;
    std::string session_dir;
    std::string raw_file;
    std::string control_file;
    double start_epoch_ms = 0.0;
    double sample_hz = 0.0;
    double poll_interval_ms = 0.0;
    double flush_every_samples = 0.0;
    bool has_start_epoch_ms = false;
    bool has_sample_hz = false;
    bool has_poll_interval_ms = false;
    bool has_flush_every_samples = false;
};

struct CameraSample
{
    Vector3 position{};
    Vector3 rotation{};
    float fov = 0.0F;
    int viewport_width = 0;
    int viewport_height = 0;
};

std::wstring WideFromUtf8(const std::string& value)
{
    if (value.empty())
    {
        return {};
    }
    const int size = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0)
    {
        return {};
    }
    std::wstring result(static_cast<std::size_t>(size), L'\0');
    if (MultiByteToWideChar(
            CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
            result.data(), size) != size)
    {
        return {};
    }
    return result;
}

void AppendUtf8CodePoint(std::string& output, std::uint32_t code_point)
{
    if (code_point <= 0x7FU)
    {
        output.push_back(static_cast<char>(code_point));
    }
    else if (code_point <= 0x7FFU)
    {
        output.push_back(static_cast<char>(0xC0U | (code_point >> 6U)));
        output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    }
    else if (code_point <= 0xFFFFU)
    {
        output.push_back(static_cast<char>(0xE0U | (code_point >> 12U)));
        output.push_back(static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
        output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    }
    else
    {
        output.push_back(static_cast<char>(0xF0U | (code_point >> 18U)));
        output.push_back(static_cast<char>(0x80U | ((code_point >> 12U) & 0x3FU)));
        output.push_back(static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
        output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    }
}

class JsonReader
{
public:
    explicit JsonReader(const std::string& text) : text_(text)
    {
        if (text_.size() >= 3 &&
            static_cast<unsigned char>(text_[0]) == 0xEFU &&
            static_cast<unsigned char>(text_[1]) == 0xBBU &&
            static_cast<unsigned char>(text_[2]) == 0xBFU)
        {
            position_ = 3;
        }
    }

    bool ReadObject(JsonFields& fields)
    {
        SkipWhitespace();
        if (!Consume('{'))
        {
            return false;
        }
        SkipWhitespace();
        if (Consume('}'))
        {
            SkipWhitespace();
            return position_ == text_.size();
        }

        while (position_ < text_.size())
        {
            std::string key;
            if (!ReadString(key))
            {
                return false;
            }
            SkipWhitespace();
            if (!Consume(':'))
            {
                return false;
            }
            SkipWhitespace();
            if (!ReadKnownValue(key, fields))
            {
                return false;
            }
            SkipWhitespace();
            if (Consume('}'))
            {
                SkipWhitespace();
                return position_ == text_.size();
            }
            if (!Consume(','))
            {
                return false;
            }
            SkipWhitespace();
        }
        return false;
    }

private:
    bool ReadKnownValue(const std::string& key, JsonFields& fields)
    {
        if (key == "status")
        {
            return ReadString(fields.status);
        }
        if (key == "session_id")
        {
            return ReadString(fields.session_id);
        }
        if (key == "session_dir")
        {
            return ReadString(fields.session_dir);
        }
        if (key == "raw_file")
        {
            return ReadString(fields.raw_file);
        }
        if (key == "control_file")
        {
            return ReadString(fields.control_file);
        }
        if (key == "start_epoch_ms")
        {
            fields.has_start_epoch_ms = ReadNumber(fields.start_epoch_ms);
            return fields.has_start_epoch_ms;
        }
        if (key == "sample_hz")
        {
            fields.has_sample_hz = ReadNumber(fields.sample_hz);
            return fields.has_sample_hz;
        }
        if (key == "poll_interval_ms")
        {
            fields.has_poll_interval_ms = ReadNumber(fields.poll_interval_ms);
            return fields.has_poll_interval_ms;
        }
        if (key == "flush_every_samples")
        {
            fields.has_flush_every_samples = ReadNumber(fields.flush_every_samples);
            return fields.has_flush_every_samples;
        }
        return SkipValue(0);
    }

    bool ReadString(std::string& output)
    {
        if (!Consume('"'))
        {
            return false;
        }
        output.clear();
        while (position_ < text_.size())
        {
            const unsigned char character = static_cast<unsigned char>(text_[position_++]);
            if (character == '"')
            {
                return true;
            }
            if (character < 0x20U)
            {
                return false;
            }
            if (character != '\\')
            {
                output.push_back(static_cast<char>(character));
                continue;
            }
            if (position_ >= text_.size())
            {
                return false;
            }
            const char escape = text_[position_++];
            switch (escape)
            {
            case '"': output.push_back('"'); break;
            case '\\': output.push_back('\\'); break;
            case '/': output.push_back('/'); break;
            case 'b': output.push_back('\b'); break;
            case 'f': output.push_back('\f'); break;
            case 'n': output.push_back('\n'); break;
            case 'r': output.push_back('\r'); break;
            case 't': output.push_back('\t'); break;
            case 'u':
            {
                std::uint32_t first = 0;
                if (!ReadHex4(first))
                {
                    return false;
                }
                std::uint32_t code_point = first;
                if (first >= 0xD800U && first <= 0xDBFFU)
                {
                    if (position_ + 2 > text_.size() ||
                        text_[position_] != '\\' || text_[position_ + 1] != 'u')
                    {
                        return false;
                    }
                    position_ += 2;
                    std::uint32_t second = 0;
                    if (!ReadHex4(second) || second < 0xDC00U || second > 0xDFFFU)
                    {
                        return false;
                    }
                    code_point = 0x10000U + ((first - 0xD800U) << 10U) + (second - 0xDC00U);
                }
                else if (first >= 0xDC00U && first <= 0xDFFFU)
                {
                    return false;
                }
                AppendUtf8CodePoint(output, code_point);
                break;
            }
            default:
                return false;
            }
        }
        return false;
    }

    bool ReadHex4(std::uint32_t& value)
    {
        if (position_ + 4 > text_.size())
        {
            return false;
        }
        value = 0;
        for (int index = 0; index < 4; ++index)
        {
            const char character = text_[position_++];
            value <<= 4U;
            if (character >= '0' && character <= '9')
            {
                value += static_cast<std::uint32_t>(character - '0');
            }
            else if (character >= 'a' && character <= 'f')
            {
                value += static_cast<std::uint32_t>(character - 'a' + 10);
            }
            else if (character >= 'A' && character <= 'F')
            {
                value += static_cast<std::uint32_t>(character - 'A' + 10);
            }
            else
            {
                return false;
            }
        }
        return true;
    }

    bool ReadNumber(double& value)
    {
        const std::size_t start = position_;
        if (position_ < text_.size() && text_[position_] == '-')
        {
            ++position_;
        }
        if (position_ >= text_.size())
        {
            return false;
        }
        if (text_[position_] == '0')
        {
            ++position_;
        }
        else if (text_[position_] >= '1' && text_[position_] <= '9')
        {
            while (position_ < text_.size() && text_[position_] >= '0' && text_[position_] <= '9')
            {
                ++position_;
            }
        }
        else
        {
            return false;
        }
        if (position_ < text_.size() && text_[position_] == '.')
        {
            ++position_;
            const std::size_t fraction = position_;
            while (position_ < text_.size() && text_[position_] >= '0' && text_[position_] <= '9')
            {
                ++position_;
            }
            if (position_ == fraction)
            {
                return false;
            }
        }
        if (position_ < text_.size() && (text_[position_] == 'e' || text_[position_] == 'E'))
        {
            ++position_;
            if (position_ < text_.size() && (text_[position_] == '+' || text_[position_] == '-'))
            {
                ++position_;
            }
            const std::size_t exponent = position_;
            while (position_ < text_.size() && text_[position_] >= '0' && text_[position_] <= '9')
            {
                ++position_;
            }
            if (position_ == exponent)
            {
                return false;
            }
        }

        try
        {
            value = std::stod(text_.substr(start, position_ - start));
            return std::isfinite(value);
        }
        catch (...)
        {
            return false;
        }
    }

    bool SkipValue(int depth)
    {
        if (depth > 32 || position_ >= text_.size())
        {
            return false;
        }
        if (text_[position_] == '"')
        {
            std::string ignored;
            return ReadString(ignored);
        }
        if (text_[position_] == '{')
        {
            ++position_;
            SkipWhitespace();
            if (Consume('}'))
            {
                return true;
            }
            while (position_ < text_.size())
            {
                std::string ignored;
                if (!ReadString(ignored))
                {
                    return false;
                }
                SkipWhitespace();
                if (!Consume(':'))
                {
                    return false;
                }
                SkipWhitespace();
                if (!SkipValue(depth + 1))
                {
                    return false;
                }
                SkipWhitespace();
                if (Consume('}'))
                {
                    return true;
                }
                if (!Consume(','))
                {
                    return false;
                }
                SkipWhitespace();
            }
            return false;
        }
        if (text_[position_] == '[')
        {
            ++position_;
            SkipWhitespace();
            if (Consume(']'))
            {
                return true;
            }
            while (position_ < text_.size())
            {
                if (!SkipValue(depth + 1))
                {
                    return false;
                }
                SkipWhitespace();
                if (Consume(']'))
                {
                    return true;
                }
                if (!Consume(','))
                {
                    return false;
                }
                SkipWhitespace();
            }
            return false;
        }
        if (MatchLiteral("true") || MatchLiteral("false") || MatchLiteral("null"))
        {
            return true;
        }
        double ignored = 0.0;
        return ReadNumber(ignored);
    }

    bool MatchLiteral(const char* literal)
    {
        const std::size_t length = std::char_traits<char>::length(literal);
        if (text_.compare(position_, length, literal) != 0)
        {
            return false;
        }
        position_ += length;
        return true;
    }

    void SkipWhitespace()
    {
        while (position_ < text_.size())
        {
            const char character = text_[position_];
            if (character != ' ' && character != '\t' && character != '\r' && character != '\n')
            {
                break;
            }
            ++position_;
        }
    }

    bool Consume(char expected)
    {
        if (position_ >= text_.size() || text_[position_] != expected)
        {
            return false;
        }
        ++position_;
        return true;
    }

    const std::string& text_;
    std::size_t position_ = 0;
};

bool ReadSharedUtf8File(const fs::path& path, std::string& output)
{
    HANDLE file = CreateFileW(
        path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE)
    {
        return false;
    }

    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 || size.QuadPart > 1024 * 1024)
    {
        CloseHandle(file);
        return false;
    }

    output.assign(static_cast<std::size_t>(size.QuadPart), '\0');
    DWORD total = 0;
    while (total < output.size())
    {
        DWORD read = 0;
        const DWORD remaining = static_cast<DWORD>(
            std::min<std::size_t>(output.size() - total, std::numeric_limits<DWORD>::max()));
        if (!ReadFile(file, output.data() + total, remaining, &read, nullptr) || read == 0)
        {
            CloseHandle(file);
            return false;
        }
        total += read;
    }
    CloseHandle(file);
    return true;
}

fs::path ModuleDirectory()
{
    std::wstring path(32768, L'\0');
    const DWORD length = GetModuleFileNameW(g_module, path.data(), static_cast<DWORD>(path.size()));
    if (length == 0 || length >= path.size())
    {
        return fs::current_path();
    }
    path.resize(length);
    return fs::path(path).parent_path();
}

fs::path ExpandPath(const std::string& utf8)
{
    const std::wstring wide = WideFromUtf8(utf8);
    if (wide.empty() && !utf8.empty())
    {
        return {};
    }
    const DWORD required = ExpandEnvironmentStringsW(wide.c_str(), nullptr, 0);
    if (required == 0)
    {
        return fs::path(wide);
    }
    std::wstring expanded(required, L'\0');
    if (ExpandEnvironmentStringsW(wide.c_str(), expanded.data(), required) == 0)
    {
        return fs::path(wide);
    }
    expanded.resize(required - 1);
    return fs::path(expanded);
}

PluginConfig LoadConfig()
{
    PluginConfig config;
    wchar_t environment_path[32768]{};
    const DWORD environment_length = GetEnvironmentVariableW(
        L"GAME_RECORDER_RDR2_CONTROL", environment_path,
        static_cast<DWORD>(std::size(environment_path)));
    if (environment_length > 0 && environment_length < std::size(environment_path))
    {
        config.control_file = environment_path;
    }

    std::string text;
    if (ReadSharedUtf8File(ModuleDirectory() / kConfigName, text))
    {
        JsonFields fields;
        if (JsonReader(text).ReadObject(fields))
        {
            if (!fields.control_file.empty())
            {
                config.control_file = ExpandPath(fields.control_file);
            }
            if (fields.has_poll_interval_ms &&
                fields.poll_interval_ms >= 20.0 && fields.poll_interval_ms <= 10000.0)
            {
                config.poll_interval_ms = static_cast<std::uint64_t>(fields.poll_interval_ms);
            }
            if (fields.has_flush_every_samples &&
                fields.flush_every_samples >= 1.0 && fields.flush_every_samples <= 10000.0)
            {
                config.flush_every_samples =
                    static_cast<std::uint64_t>(fields.flush_every_samples);
            }
        }
    }
    return config;
}

std::int64_t UnixTimeMilliseconds()
{
    FILETIME file_time{};
    using PreciseTimeFunction = VOID(WINAPI*)(LPFILETIME);
    static const auto precise_time = reinterpret_cast<PreciseTimeFunction>(
        GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "GetSystemTimePreciseAsFileTime"));
    if (precise_time != nullptr)
    {
        precise_time(&file_time);
    }
    else
    {
        GetSystemTimeAsFileTime(&file_time);
    }
    ULARGE_INTEGER ticks{};
    ticks.LowPart = file_time.dwLowDateTime;
    ticks.HighPart = file_time.dwHighDateTime;
    return static_cast<std::int64_t>((ticks.QuadPart - kFileTimeUnixEpoch) / 10000ULL);
}

std::string EscapeJson(const std::string& value)
{
    std::ostringstream escaped;
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"': escaped << "\\\""; break;
        case '\\': escaped << "\\\\"; break;
        case '\b': escaped << "\\b"; break;
        case '\f': escaped << "\\f"; break;
        case '\n': escaped << "\\n"; break;
        case '\r': escaped << "\\r"; break;
        case '\t': escaped << "\\t"; break;
        default:
            if (character < 0x20U)
            {
                escaped << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<unsigned int>(character) << std::dec;
            }
            else
            {
                escaped << static_cast<char>(character);
            }
        }
    }
    return escaped.str();
}

std::string FormatFloat(double value)
{
    if (!std::isfinite(value))
    {
        return "0";
    }
    std::ostringstream stream;
    stream.imbue(std::locale::classic());
    stream << std::setprecision(9) << value;
    return stream.str();
}

HWND FindGameWindow()
{
    const DWORD process_id = GetCurrentProcessId();
    struct Search
    {
        DWORD process_id;
        HWND best = nullptr;
        std::int64_t best_area = 0;
    } search{process_id};

    EnumWindows(
        [](HWND window, LPARAM parameter) -> BOOL
        {
            auto& state = *reinterpret_cast<Search*>(parameter);
            DWORD window_process_id = 0;
            GetWindowThreadProcessId(window, &window_process_id);
            if (window_process_id != state.process_id || !IsWindowVisible(window) ||
                GetWindow(window, GW_OWNER) != nullptr)
            {
                return TRUE;
            }
            RECT client{};
            if (!GetClientRect(window, &client))
            {
                return TRUE;
            }
            const std::int64_t area =
                static_cast<std::int64_t>(client.right - client.left) *
                static_cast<std::int64_t>(client.bottom - client.top);
            if (area > state.best_area)
            {
                state.best = window;
                state.best_area = area;
            }
            return TRUE;
        },
        reinterpret_cast<LPARAM>(&search));
    return search.best;
}

Vector3 GetFinalRenderedCamCoord()
{
    return invoke<Vector3>(0x5352E025EC2B416FULL);
}

Vector3 GetFinalRenderedCamRot()
{
    return invoke<Vector3>(0x602685BD85DD26CAULL, 2);
}

float GetFinalRenderedCamFov()
{
    return invoke<float>(0x04AF77971E508F6AULL);
}

bool ReadCameraSample(HWND& game_window, CameraSample& sample)
{
    sample.position = GetFinalRenderedCamCoord();
    sample.rotation = GetFinalRenderedCamRot();
    sample.fov = GetFinalRenderedCamFov();

    if (game_window == nullptr || !IsWindow(game_window))
    {
        game_window = FindGameWindow();
    }
    RECT client{};
    if (game_window == nullptr || !GetClientRect(game_window, &client))
    {
        game_window = FindGameWindow();
        if (game_window == nullptr || !GetClientRect(game_window, &client))
        {
            return false;
        }
    }
    sample.viewport_width = client.right - client.left;
    sample.viewport_height = client.bottom - client.top;
    const auto finite = [](float value) { return std::isfinite(static_cast<double>(value)); };
    return sample.viewport_width > 0 && sample.viewport_height > 0 &&
           finite(sample.position.x) && finite(sample.position.y) && finite(sample.position.z) &&
           finite(sample.rotation.x) && finite(sample.rotation.y) && finite(sample.rotation.z) &&
           finite(sample.fov) && sample.fov > 0.0F;
}

void AppendCameraToWorld(std::ostringstream& line, const CameraSample& sample)
{
    constexpr double degrees_to_radians = 3.14159265358979323846 / 180.0;
    const double pitch = static_cast<double>(sample.rotation.x) * degrees_to_radians;
    const double roll = static_cast<double>(sample.rotation.y) * degrees_to_radians;
    const double yaw = static_cast<double>(sample.rotation.z) * degrees_to_radians;
    const double sx = std::sin(pitch);
    const double cx = std::cos(pitch);
    const double sy = std::sin(roll);
    const double cy = std::cos(roll);
    const double sz = std::sin(yaw);
    const double cz = std::cos(yaw);

    // For rotationOrder=2, compose column-vector rotations as Rz(yaw) *
    // Rx(pitch) * Ry(roll). Rows below are the resulting local camera basis
    // vectors in world coordinates for p_world = p_camera * C2W.
    const double matrix[16] = {
        cz * cy - sz * sx * sy, sz * cy + cz * sx * sy, -cx * sy, 0.0,
        -sz * cx, cz * cx, sx, 0.0,
        cz * sy + sz * sx * cy, sz * sy - cz * sx * cy, cx * cy, 0.0,
        sample.position.x, sample.position.y, sample.position.z, 1.0};

    line << ",\"camera_to_world\":[";
    for (int index = 0; index < 16; ++index)
    {
        if (index != 0)
        {
            line << ',';
        }
        line << FormatFloat(matrix[index]);
    }
    line << ']';
}

bool ParseControl(const std::string& text, ControlState& control)
{
    JsonFields fields;
    if (!JsonReader(text).ReadObject(fields))
    {
        return false;
    }
    control.status = fields.status;
    control.session_id = fields.session_id;
    control.session_dir = fields.session_dir;
    if (!fields.raw_file.empty())
    {
        control.raw_file = fields.raw_file;
    }
    if (fields.has_start_epoch_ms &&
        fields.start_epoch_ms >= 0.0 &&
        fields.start_epoch_ms <= static_cast<double>(std::numeric_limits<std::int64_t>::max()))
    {
        control.start_epoch_ms = static_cast<std::int64_t>(fields.start_epoch_ms);
        control.has_start_epoch_ms = true;
    }
    if (fields.has_sample_hz && fields.sample_hz >= 1.0 && fields.sample_hz <= 1000.0)
    {
        control.sample_hz = fields.sample_hz;
    }
    return true;
}

bool IsRecordingStatus(const std::string& status)
{
    if (status.size() != 9)
    {
        return false;
    }
    const char expected[] = "recording";
    for (std::size_t index = 0; index < status.size(); ++index)
    {
        if (static_cast<char>(std::tolower(static_cast<unsigned char>(status[index]))) != expected[index])
        {
            return false;
        }
    }
    return true;
}

class Recorder
{
public:
    explicit Recorder(PluginConfig config) : config_(std::move(config))
    {
    }

    ~Recorder()
    {
        Stop("script unload");
    }

    void Tick()
    {
        const std::uint64_t now_ticks = GetTickCount64();
        if (now_ticks >= next_poll_ticks_)
        {
            next_poll_ticks_ = now_ticks + config_.poll_interval_ms;
            PollControl();
        }

        if (!recording_)
        {
            return;
        }

        CameraSample sample;
        if (!ReadCameraSample(game_window_, sample))
        {
            return;
        }
        if (now_ticks < next_sample_ticks_)
        {
            return;
        }
        const double period = 1000.0 / sample_hz_;
        next_sample_ticks_ = now_ticks + std::max<std::uint64_t>(
            1, static_cast<std::uint64_t>(std::llround(period)));
        WriteSample(sample);
    }

private:
    void PollControl()
    {
        if (config_.control_file.empty())
        {
            if (recording_)
            {
                Stop("control path unavailable");
            }
            return;
        }

        std::string text;
        if (!ReadSharedUtf8File(config_.control_file, text))
        {
            if (recording_)
            {
                Stop("control removed");
            }
            return;
        }

        ControlState control;
        if (!ParseControl(text, control))
        {
            return; // An atomic replacement may be observed between writes; retry next poll.
        }
        if (control.status.empty())
        {
            return;
        }
        if (!IsRecordingStatus(control.status))
        {
            if (recording_)
            {
                Stop("recorder idle");
            }
            return;
        }
        if (control.session_dir.empty())
        {
            return;
        }

        const fs::path raw_name = WideFromUtf8(control.raw_file);
        if (raw_name.empty() || raw_name.is_absolute() || raw_name.has_parent_path() ||
            raw_name.filename() != raw_name)
        {
            return;
        }
        const fs::path session_dir = ExpandPath(control.session_dir);
        if (session_dir.empty())
        {
            return;
        }
        const fs::path output_path = (session_dir / raw_name).lexically_normal();
        if (recording_ && control.session_id == active_session_id_ && output_path == active_path_)
        {
            sample_hz_ = control.sample_hz;
            return;
        }
        if (recording_)
        {
            Stop("session switched");
        }
        Start(
            output_path, control.session_id,
            control.has_start_epoch_ms ? control.start_epoch_ms : UnixTimeMilliseconds(),
            control.sample_hz);
    }

    void Start(
        const fs::path& path, const std::string& session_id,
        std::int64_t start_epoch_ms, double sample_hz)
    {
        std::error_code error;
        fs::create_directories(path.parent_path(), error);
        if (error)
        {
            return;
        }

        writer_.open(path, std::ios::binary | std::ios::trunc);
        if (!writer_)
        {
            return;
        }

        active_path_ = path;
        active_session_id_ = session_id;
        sample_hz_ = sample_hz;
        sample_count_ = 0;
        next_sample_ticks_ = 0;
        recording_ = true;

        writer_
            << "{\"type\":\"header\""
            << ",\"schema\":\"rdr2_camera_v1\""
            << ",\"start_unix_ms\":" << start_epoch_ms
            << ",\"sample_hz\":" << FormatFloat(sample_hz_)
            << ",\"session_id\":\"" << EscapeJson(active_session_id_) << '"'
            << ",\"world_units\":\"meters\""
            << ",\"camera_to_world_translation_units\":\"meters\""
            << ",\"matrix_layout\":\"row_major\""
            << ",\"matrix_vector_convention\":\"row_vector\""
            << ",\"world_axes\":\"x_right_y_forward_z_up\""
            << ",\"camera_axes\":\"x_right_y_forward_z_up\""
            << ",\"camera_to_world_source\":\"final_rendered_cam_coord_rot_order_2\""
            << ",\"sample_policy\":\"final_rendered_camera\""
            << ",\"fov_axis\":\"vertical\""
            << ",\"projection_source\":\"final_rendered_cam_fov_plus_client_rect\""
            << "}\n";
        writer_.flush();
        if (!writer_)
        {
            Stop("header write error");
        }
    }

    void WriteSample(const CameraSample& sample)
    {
        std::ostringstream line;
        line.imbue(std::locale::classic());
        line << "{\"type\":\"sample\",\"t_unix_ms\":" << UnixTimeMilliseconds();
        AppendCameraToWorld(line, sample);
        line << ",\"fov_vertical_deg\":" << FormatFloat(sample.fov)
             << ",\"viewport_px\":[" << sample.viewport_width << ',' << sample.viewport_height << "]}\n";
        writer_ << line.str();
        if (!writer_)
        {
            Stop("sample write error");
            return;
        }
        ++sample_count_;
        if (sample_count_ % config_.flush_every_samples == 0)
        {
            writer_.flush();
            if (!writer_)
            {
                Stop("flush error");
            }
        }
    }

    void Stop(const char* reason)
    {
        if (!recording_)
        {
            return;
        }
        if (writer_)
        {
            writer_ << "{\"type\":\"footer\",\"end_unix_ms\":" << UnixTimeMilliseconds()
                    << ",\"sample_count\":" << sample_count_
                    << ",\"reason\":\"" << EscapeJson(reason) << "\"}\n";
            writer_.flush();
        }
        writer_.close();
        recording_ = false;
        active_path_.clear();
        active_session_id_.clear();
        sample_count_ = 0;
    }

    PluginConfig config_;
    bool recording_ = false;
    std::ofstream writer_;
    fs::path active_path_;
    std::string active_session_id_;
    double sample_hz_ = 30.0;
    std::uint64_t sample_count_ = 0;
    std::uint64_t next_poll_ticks_ = 0;
    std::uint64_t next_sample_ticks_ = 0;
    HWND game_window_ = nullptr;
};
} // namespace

void ScriptMain()
{
    Recorder recorder(LoadConfig());
    while (true)
    {
        recorder.Tick();
        WAIT(0);
    }
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID)
{
    switch (reason)
    {
    case DLL_PROCESS_ATTACH:
        g_module = module;
        DisableThreadLibraryCalls(module);
        scriptRegister(module, ScriptMain);
        break;
    case DLL_PROCESS_DETACH:
        scriptUnregister(module);
        break;
    default:
        break;
    }
    return TRUE;
}
