using System;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Forms;
using GTA;
using GTA.Math;

/// <summary>
/// Samples GTA V gameplay-camera pose.
///
/// Default mode (follow_recorder=true): watches active_session.json written by
/// game-recorder. When a session starts, writes camera_raw_gta.jsonl into that
/// session folder; when recording ends, stops. No manual F10 needed.
///
/// Optional manual toggle still available if configured.
/// </summary>
public class CameraPoseLogger : Script
{
    private const string ConfigFileName = "camera_pose_logger.config.json";
    private const string DefaultRawName = "camera_raw_gta.jsonl";

    private string _outputDir;
    private string _controlFile;
    private double _sampleHz = 30.0;
    private bool _followRecorder = true;
    private Keys? _toggleKey = null; // null = disabled (default when following)
    private Keys _flushKey = Keys.F9;
    private bool _includeMatrix = true;
    private bool _includePlayer = true;

    private bool _recording;
    private StreamWriter _writer;
    private string _activePath;
    private string _activeSessionId = "";
    private DateTime _nextSampleUtc = DateTime.MinValue;
    private DateTime _nextPollUtc = DateTime.MinValue;
    private long _sampleCount;
    private long _segmentStartUnixMs;
    private DateTime _lastControlWriteUtc = DateTime.MinValue;

    public CameraPoseLogger()
    {
        LoadConfig();
        try
        {
            Directory.CreateDirectory(_outputDir);
        }
        catch
        {
            // ignore — will retry when starting
        }

        Interval = 0;
        Tick += OnTick;
        KeyDown += OnKeyDown;

        var mode = _followRecorder ? "follow game-recorder" : "manual";
        Notify("CameraPose ready (" + mode + "). ctrl=" + _controlFile);
    }

    private void OnKeyDown(object sender, KeyEventArgs e)
    {
        if (_toggleKey.HasValue && e.KeyCode == _toggleKey.Value && !_followRecorder)
        {
            if (_recording)
            {
                StopRecording("manual off");
            }
            else
            {
                StartRecordingTo(
                    Path.Combine(
                        _outputDir,
                        "gta_camera_"
                            + DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture)
                            + ".jsonl"
                    ),
                    sessionId: "",
                    startEpochMs: ToUnixMs(DateTime.UtcNow)
                );
            }
            return;
        }

        if (e.KeyCode == _flushKey && _recording)
        {
            try
            {
                if (_writer != null)
                {
                    _writer.Flush();
                }
                Notify("Flushed " + _sampleCount + " samples");
            }
            catch (Exception ex)
            {
                Notify("Flush failed: " + ex.Message);
            }
        }
    }

    private void OnTick(object sender, EventArgs e)
    {
        var now = DateTime.UtcNow;

        if (_followRecorder && now >= _nextPollUtc)
        {
            _nextPollUtc = now.AddMilliseconds(100);
            PollControlFile(now);
        }

        if (!_recording || _writer == null)
        {
            return;
        }

        if (now < _nextSampleUtc)
        {
            return;
        }

        var periodMs = 1000.0 / Math.Max(1.0, _sampleHz);
        _nextSampleUtc = now.AddMilliseconds(periodMs);

        try
        {
            WriteSample(now);
        }
        catch (Exception ex)
        {
            Notify("Write failed, stopping: " + ex.Message);
            StopRecording("write error");
        }
    }

    private void PollControlFile(DateTime now)
    {
        if (string.IsNullOrWhiteSpace(_controlFile) || !File.Exists(_controlFile))
        {
            if (_recording && !string.IsNullOrEmpty(_activeSessionId))
            {
                StopRecording("control removed");
            }
            return;
        }

        DateTime writeUtc;
        try
        {
            writeUtc = File.GetLastWriteTimeUtc(_controlFile);
        }
        catch
        {
            return;
        }

        // Always re-read when mtime changes; also re-read periodically in case
        // atomic replace keeps similar stamp on some FS.
        if (writeUtc == _lastControlWriteUtc && (now - _lastControlWriteUtc).TotalSeconds < 1.0)
        {
            return;
        }
        _lastControlWriteUtc = writeUtc;

        string text;
        try
        {
            text = File.ReadAllText(_controlFile, Encoding.UTF8);
        }
        catch
        {
            return;
        }

        var status = ExtractJsonString(text, "status") ?? "";
        if (!string.Equals(status, "recording", StringComparison.OrdinalIgnoreCase))
        {
            if (_recording)
            {
                StopRecording("recorder idle");
            }
            return;
        }

        var sessionId = ExtractJsonString(text, "session_id") ?? "";
        var sessionDir = ExtractJsonString(text, "session_dir") ?? "";
        var rawName = ExtractJsonString(text, "raw_file") ?? DefaultRawName;
        var startMs = ExtractJsonNumber(text, "start_epoch_ms");
        var hz = ExtractJsonNumber(text, "sample_hz");
        if (hz.HasValue && hz.Value > 0)
        {
            _sampleHz = hz.Value;
        }

        if (string.IsNullOrWhiteSpace(sessionDir))
        {
            return;
        }

        sessionDir = Environment.ExpandEnvironmentVariables(sessionDir.Trim());
        var outPath = Path.Combine(sessionDir, rawName);

        if (_recording && sessionId == _activeSessionId && _activePath == outPath)
        {
            return;
        }

        if (_recording)
        {
            StopRecording("session switched");
        }

        StartRecordingTo(
            outPath,
            sessionId: sessionId,
            startEpochMs: startMs.HasValue ? (long)startMs.Value : ToUnixMs(DateTime.UtcNow)
        );
    }

    private void StartRecordingTo(string path, string sessionId, long startEpochMs)
    {
        try
        {
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
            {
                Directory.CreateDirectory(dir);
            }

            _activePath = path;
            _activeSessionId = sessionId ?? "";
            _writer = new StreamWriter(path, false, Encoding.UTF8)
            {
                AutoFlush = false,
            };
            _segmentStartUnixMs = startEpochMs;
            _sampleCount = 0;
            _nextSampleUtc = DateTime.MinValue;
            _recording = true;

            var header = new StringBuilder(320);
            header.Append("{\"type\":\"header\"");
            header.Append(",\"schema\":\"gta_camera_v1\"");
            header.Append(",\"start_unix_ms\":").Append(_segmentStartUnixMs);
            header.Append(",\"sample_hz\":").Append(
                _sampleHz.ToString("0.###", CultureInfo.InvariantCulture)
            );
            header.Append(",\"session_id\":\"").Append(EscapeJson(_activeSessionId)).Append('"');
            header.Append(",\"units\":\"gta_world_meters_deg\"");
            header.Append(",\"rot_order\":\"pitch_roll_yaw_deg\"");
            header.Append('}');
            _writer.WriteLine(header.ToString());
            _writer.Flush();

            Notify("Camera ON → " + Path.GetFileName(dir) + "/" + Path.GetFileName(path));
        }
        catch (Exception ex)
        {
            _recording = false;
            _writer = null;
            _activePath = null;
            _activeSessionId = "";
            Notify("Start failed: " + ex.Message);
        }
    }

    private void StopRecording(string reason)
    {
        if (!_recording)
        {
            return;
        }

        var count = _sampleCount;
        try
        {
            if (_writer != null)
            {
                var endMs = ToUnixMs(DateTime.UtcNow);
                var footer = new StringBuilder(192);
                footer.Append("{\"type\":\"footer\"");
                footer.Append(",\"end_unix_ms\":").Append(endMs);
                footer.Append(",\"sample_count\":").Append(count);
                footer.Append(",\"reason\":\"").Append(EscapeJson(reason)).Append('"');
                footer.Append('}');
                _writer.WriteLine(footer.ToString());
                _writer.Flush();
                _writer.Dispose();
            }
        }
        catch
        {
            // ignore close errors
        }

        _writer = null;
        _recording = false;
        _activePath = null;
        _activeSessionId = "";
        _sampleCount = 0;
        Notify("Camera OFF (" + count + " samples, " + reason + ")");
    }

    private void WriteSample(DateTime utcNow)
    {
        var unixMs = ToUnixMs(utcNow);
        var pos = GameplayCamera.Position;
        var rot = GameplayCamera.Rotation;
        var fov = GameplayCamera.FieldOfView;

        var sb = new StringBuilder(384);
        sb.Append("{\"type\":\"sample\"");
        sb.Append(",\"t_unix_ms\":").Append(unixMs);
        AppendVec3(sb, "pos", pos);
        AppendVec3(sb, "rot", rot);
        sb.Append(",\"fov\":").Append(FormatFloat(fov));

        if (_includeMatrix)
        {
            // Do NOT call GET_GAMEPLAY_CAM_MATRIX via a raw hash cast — truncating
            // 0x814C9E6433E1A3BF to int becomes 0x33E1A3BF and ScriptHookV fatals with
            // "Can't find native". Prefer SHVDN API vectors instead.
            try
            {
                AppendVec3(sb, "forward", GameplayCamera.Direction);
            }
            catch
            {
                // Direction unavailable on some builds — pos/rot/fov are enough.
            }
        }

        if (_includePlayer && Game.Player != null && Game.Player.Character != null)
        {
            var ped = Game.Player.Character;
            AppendVec3(sb, "player_pos", ped.Position);
            sb.Append(",\"player_heading\":").Append(FormatFloat(ped.Heading));
        }

        var rendering = false;
        try
        {
            rendering = GameplayCamera.IsRendering;
        }
        catch
        {
            rendering = true;
        }
        sb.Append(",\"cam_rendering\":").Append(rendering ? "true" : "false");
        sb.Append('}');

        _writer.WriteLine(sb.ToString());
        _sampleCount++;

        if ((_sampleCount % 30) == 0)
        {
            _writer.Flush();
        }
    }

    private void LoadConfig()
    {
        var scriptDir = GetScriptDirectory();
        var configPath = Path.Combine(scriptDir, ConfigFileName);
        if (!File.Exists(configPath))
        {
            var alt = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, ConfigFileName);
            if (File.Exists(alt))
            {
                configPath = alt;
            }
        }

        _outputDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            "game-recorder",
            "gta_camera"
        );
        _controlFile = Path.Combine(_outputDir, "active_session.json");
        _followRecorder = true;
        _toggleKey = null;

        if (!File.Exists(configPath))
        {
            TryWriteDefaultConfig(Path.Combine(scriptDir, ConfigFileName));
            return;
        }

        try
        {
            var text = File.ReadAllText(configPath, Encoding.UTF8);
            var outputDir = ExtractJsonString(text, "output_dir");
            if (!string.IsNullOrWhiteSpace(outputDir))
            {
                _outputDir = Environment.ExpandEnvironmentVariables(outputDir.Trim());
            }

            var control = ExtractJsonString(text, "control_file");
            if (!string.IsNullOrWhiteSpace(control))
            {
                _controlFile = Environment.ExpandEnvironmentVariables(control.Trim());
            }
            else
            {
                _controlFile = Path.Combine(_outputDir, "active_session.json");
            }

            var hz = ExtractJsonNumber(text, "sample_hz");
            if (hz.HasValue && hz.Value > 0)
            {
                _sampleHz = hz.Value;
            }

            var follow = ExtractJsonBool(text, "follow_recorder");
            if (follow.HasValue)
            {
                _followRecorder = follow.Value;
            }

            var toggle = ExtractJsonString(text, "toggle_key");
            if (!string.IsNullOrWhiteSpace(toggle)
                && !string.Equals(toggle.Trim(), "none", StringComparison.OrdinalIgnoreCase)
                && !string.Equals(toggle.Trim(), "null", StringComparison.OrdinalIgnoreCase)
                && !string.Equals(toggle.Trim(), "", StringComparison.OrdinalIgnoreCase))
            {
                Keys parsed;
                if (Enum.TryParse(toggle.Trim(), true, out parsed))
                {
                    _toggleKey = parsed;
                }
            }

            var flush = ExtractJsonString(text, "flush_key");
            if (!string.IsNullOrWhiteSpace(flush))
            {
                Keys parsed;
                if (Enum.TryParse(flush.Trim(), true, out parsed))
                {
                    _flushKey = parsed;
                }
            }

            var includeMatrix = ExtractJsonBool(text, "include_matrix");
            if (includeMatrix.HasValue)
            {
                _includeMatrix = includeMatrix.Value;
            }

            var includePlayer = ExtractJsonBool(text, "include_player");
            if (includePlayer.HasValue)
            {
                _includePlayer = includePlayer.Value;
            }
        }
        catch (Exception ex)
        {
            Notify("Config parse error: " + ex.Message);
        }
    }

    private static void TryWriteDefaultConfig(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                return;
            }

            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
            {
                Directory.CreateDirectory(dir);
            }

            var docs = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "game-recorder",
                "gta_camera"
            ).Replace("\\", "/");
            var body =
                "{\n"
                + "  \"output_dir\": \""
                + docs
                + "\",\n"
                + "  \"control_file\": \""
                + docs
                + "/active_session.json\",\n"
                + "  \"follow_recorder\": true,\n"
                + "  \"sample_hz\": 30,\n"
                + "  \"toggle_key\": \"none\",\n"
                + "  \"flush_key\": \"F9\",\n"
                + "  \"include_matrix\": true,\n"
                + "  \"include_player\": true\n"
                + "}\n";
            File.WriteAllText(path, body, Encoding.UTF8);
        }
        catch
        {
            // ignore
        }
    }

    private static string GetScriptDirectory()
    {
        try
        {
            var loc = typeof(CameraPoseLogger).Assembly.Location;
            if (!string.IsNullOrEmpty(loc))
            {
                return Path.GetDirectoryName(loc) ?? ".";
            }
        }
        catch
        {
            // ignore
        }
        return AppDomain.CurrentDomain.BaseDirectory ?? ".";
    }

    private static long ToUnixMs(DateTime utc)
    {
        if (utc.Kind != DateTimeKind.Utc)
        {
            utc = utc.ToUniversalTime();
        }
        return (long)(utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalMilliseconds;
    }

    private static void AppendVec3(StringBuilder sb, string key, Vector3 v)
    {
        sb.Append(",\"").Append(key).Append("\":[");
        sb.Append(FormatFloat(v.X)).Append(',');
        sb.Append(FormatFloat(v.Y)).Append(',');
        sb.Append(FormatFloat(v.Z)).Append(']');
    }

    private static string FormatFloat(float v)
    {
        if (float.IsNaN(v) || float.IsInfinity(v))
        {
            return "0";
        }
        return v.ToString("0.######", CultureInfo.InvariantCulture);
    }

    private static string EscapeJson(string s)
    {
        if (string.IsNullOrEmpty(s))
        {
            return "";
        }
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    private static string ExtractJsonString(string json, string key)
    {
        var needle = "\"" + key + "\"";
        var i = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
        if (i < 0)
        {
            return null;
        }
        i = json.IndexOf(':', i + needle.Length);
        if (i < 0)
        {
            return null;
        }
        i++;
        while (i < json.Length && char.IsWhiteSpace(json[i]))
        {
            i++;
        }
        if (i >= json.Length || json[i] != '"')
        {
            return null;
        }
        i++;
        var sb = new StringBuilder();
        while (i < json.Length)
        {
            var c = json[i++];
            if (c == '\\' && i < json.Length)
            {
                sb.Append(json[i++]);
                continue;
            }
            if (c == '"')
            {
                break;
            }
            sb.Append(c);
        }
        return sb.ToString();
    }

    private static double? ExtractJsonNumber(string json, string key)
    {
        var needle = "\"" + key + "\"";
        var i = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
        if (i < 0)
        {
            return null;
        }
        i = json.IndexOf(':', i + needle.Length);
        if (i < 0)
        {
            return null;
        }
        i++;
        while (i < json.Length && char.IsWhiteSpace(json[i]))
        {
            i++;
        }
        var start = i;
        while (
            i < json.Length
            && (
                char.IsDigit(json[i])
                || json[i] == '-'
                || json[i] == '+'
                || json[i] == '.'
                || json[i] == 'e'
                || json[i] == 'E'
            )
        )
        {
            i++;
        }
        if (i <= start)
        {
            return null;
        }
        double v;
        if (
            double.TryParse(
                json.Substring(start, i - start),
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out v
            )
        )
        {
            return v;
        }
        return null;
    }

    private static bool? ExtractJsonBool(string json, string key)
    {
        var needle = "\"" + key + "\"";
        var i = json.IndexOf(needle, StringComparison.OrdinalIgnoreCase);
        if (i < 0)
        {
            return null;
        }
        i = json.IndexOf(':', i + needle.Length);
        if (i < 0)
        {
            return null;
        }
        i++;
        while (i < json.Length && char.IsWhiteSpace(json[i]))
        {
            i++;
        }
        if (json.IndexOf("true", i, StringComparison.OrdinalIgnoreCase) == i)
        {
            return true;
        }
        if (json.IndexOf("false", i, StringComparison.OrdinalIgnoreCase) == i)
        {
            return false;
        }
        return null;
    }

    private static void Notify(string msg)
    {
        var text = "~b~CameraPose~w~ " + msg;
        try
        {
            GTA.UI.Notification.PostTicker(text, false);
            return;
        }
        catch
        {
            // fall through
        }
        try
        {
            GTA.UI.Screen.ShowSubtitle(text, 3500);
        }
        catch
        {
            // ignore
        }
    }
}
