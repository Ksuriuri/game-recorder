#include <reshade.hpp>

#include "CP2077DepthMath.hpp"

#include <Windows.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
using namespace reshade::api;

namespace
{
constexpr size_t kReadbackSlots = 4;
constexpr size_t kWriterQueueLimit = 4;
constexpr auto kControlPollPeriod = std::chrono::milliseconds(100);
constexpr char kEffectFilename[] = "CP2077Depth.fx";
constexpr char kTechniqueName[] = "CP2077DepthCopy";
constexpr char kTextureName[] = "CP2077DepthExport";

fs::path g_control_path;

void log_message(const reshade::log::level level, const std::string &message)
{
	reshade::log::message(level, message.c_str());
}

int64_t unix_ms_now()
{
	return std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string json_escape(const std::string &value)
{
	std::string out;
	out.reserve(value.size() + 8);
	for (const unsigned char ch : value)
	{
		switch (ch)
		{
		case '\\': out += "\\\\"; break;
		case '"': out += "\\\""; break;
		case '\b': out += "\\b"; break;
		case '\f': out += "\\f"; break;
		case '\n': out += "\\n"; break;
		case '\r': out += "\\r"; break;
		case '\t': out += "\\t"; break;
		default:
			if (ch < 0x20)
			{
				char escaped[7] = {};
				std::snprintf(escaped, sizeof(escaped), "\\u%04x", ch);
				out += escaped;
			}
			else
			{
				out.push_back(static_cast<char>(ch));
			}
			break;
		}
	}
	return out;
}

size_t find_json_value(const std::string &text, const std::string &key)
{
	const std::string needle = "\"" + key + "\"";
	size_t pos = text.find(needle);
	if (pos == std::string::npos)
		return pos;
	pos = text.find(':', pos + needle.size());
	if (pos == std::string::npos)
		return pos;
	do
	{
		++pos;
	} while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos])));
	return pos;
}

std::optional<std::string> json_string_value(const std::string &text, const std::string &key)
{
	size_t pos = find_json_value(text, key);
	if (pos == std::string::npos || pos >= text.size() || text[pos] != '"')
		return std::nullopt;

	std::string out;
	for (++pos; pos < text.size(); ++pos)
	{
		const char ch = text[pos];
		if (ch == '"')
			return out;
		if (ch != '\\')
		{
			out.push_back(ch);
			continue;
		}
		if (++pos >= text.size())
			return std::nullopt;
		switch (text[pos])
		{
		case '"': out.push_back('"'); break;
		case '\\': out.push_back('\\'); break;
		case '/': out.push_back('/'); break;
		case 'b': out.push_back('\b'); break;
		case 'f': out.push_back('\f'); break;
		case 'n': out.push_back('\n'); break;
		case 'r': out.push_back('\r'); break;
		case 't': out.push_back('\t'); break;
		default: return std::nullopt;
		}
	}
	return std::nullopt;
}

std::optional<double> json_number_value(const std::string &text, const std::string &key)
{
	const size_t pos = find_json_value(text, key);
	if (pos == std::string::npos || pos >= text.size())
		return std::nullopt;
	char *end = nullptr;
	const double value = std::strtod(text.c_str() + pos, &end);
	if (end == text.c_str() + pos || !std::isfinite(value))
		return std::nullopt;
	return value;
}

struct SessionControl
{
	std::string status;
	std::string session_id;
	std::string session_dir;
	double sample_hz = 5.0;
};

std::optional<SessionControl> read_control()
{
	std::ifstream input(g_control_path, std::ios::binary);
	if (!input)
		return std::nullopt;
	const std::string text(
		(std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
	const auto status = json_string_value(text, "status");
	if (!status)
		return std::nullopt;

	SessionControl control;
	control.status = *status;
	control.session_id = json_string_value(text, "session_id").value_or("");
	control.session_dir = json_string_value(text, "session_dir").value_or("");
	control.sample_hz = std::clamp(
		json_number_value(text, "sample_hz").value_or(5.0), 0.1, 60.0);
	return control;
}

bool write_npy_f32(
	const fs::path &target,
	const uint32_t width,
	const uint32_t height,
	const std::vector<uint32_t> &float_bits)
{
	std::ostringstream dict_builder;
	dict_builder << "{'descr': '<f4', 'fortran_order': False, 'shape': ("
		<< height << ", " << width << "), }";
	std::string header = dict_builder.str();
	const size_t preamble_size = 10;
	const size_t padding = (64 - ((preamble_size + header.size() + 1) % 64)) % 64;
	header.append(padding, ' ');
	header.push_back('\n');
	if (header.size() > std::numeric_limits<uint16_t>::max())
		return false;

	fs::path temporary = target;
	temporary += ".tmp";
	std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
	if (!output)
		return false;

	const char magic[] = {
		static_cast<char>(0x93), 'N', 'U', 'M', 'P', 'Y', 1, 0
	};
	const uint16_t header_size = static_cast<uint16_t>(header.size());
	const char header_size_le[] = {
		static_cast<char>(header_size & 0xff),
		static_cast<char>((header_size >> 8) & 0xff)
	};
	output.write(magic, sizeof(magic));
	output.write(header_size_le, sizeof(header_size_le));
	output.write(header.data(), static_cast<std::streamsize>(header.size()));
	output.write(
		reinterpret_cast<const char *>(float_bits.data()),
		static_cast<std::streamsize>(float_bits.size() * sizeof(uint32_t)));
	output.close();
	if (!output)
	{
		std::error_code ignored;
		fs::remove(temporary, ignored);
		return false;
	}

	if (!MoveFileExW(
		temporary.c_str(), target.c_str(),
		MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
	{
		std::error_code ignored;
		fs::remove(temporary, ignored);
		return false;
	}
	return true;
}

class SessionOutput
{
public:
	SessionOutput(std::string session_id, fs::path session_dir, const double sample_hz)
		: session_id_(std::move(session_id)), session_dir_(std::move(session_dir))
	{
		std::error_code error;
		depth_dir_ = session_dir_ / "depth";
		fs::create_directories(depth_dir_, error);
		if (error)
			return;

		index_.open(session_dir_ / "depth_raw_cp2077.jsonl", std::ios::trunc);
		if (!index_)
			return;
		index_ << "{\"type\":\"header\",\"schema\":\"cp2077_depth_v1\""
			<< ",\"session_id\":\"" << json_escape(session_id_) << "\""
			<< ",\"definition\":\"OpenCV camera-coordinate optical-axis value Zc\""
			<< ",\"camera_axes\":\"x_right_y_down_z_forward\""
			<< ",\"units\":\"m\",\"dtype\":\"<f4\",\"array_layout\":\"H_W\""
			<< ",\"sample_hz\":" << std::setprecision(10) << sample_hz
			<< ",\"source\":\"reshade_depth_semantic_via_r32_float\""
			<< ",\"clock\":\"system_clock_unix_ms_at_gpu_copy_submit\""
			<< ",\"calibration\":{\"kind\":\"empirical_cp2077_device_depth_to_zc\""
			<< ",\"provenance\":\"jasonbunk/reshade_cv Cyberpunk2077 curve\""
			<< ",\"formula\":\"1.28/(0.000077579959+exp(354.9329993*uint_bits(device_depth)/4294967295-83.84035513))\""
			<< ",\"numerator\":1.28,\"offset\":0.000077579959"
			<< ",\"exponent_scale\":354.9329993,\"exponent_bias\":-83.84035513}}\n";
		index_.flush();
		valid_ = static_cast<bool>(index_);
	}

	~SessionOutput()
	{
		if (!index_)
			return;
		index_ << "{\"type\":\"footer\",\"written_samples\":" << written_samples_.load()
			<< ",\"dropped_gpu\":" << dropped_gpu.load()
			<< ",\"dropped_writer\":" << dropped_writer.load() << "}\n";
		index_.flush();
	}

	bool valid() const noexcept { return valid_; }
	uint64_t allocate_sequence() noexcept { return next_sequence_++; }

	void write_sample(
		const uint64_t sequence,
		const int64_t timestamp_ms,
		const uint32_t width,
		const uint32_t height,
		std::vector<uint32_t> device_depth_bits)
	{
		uint64_t valid_pixels = 0;
		for (uint32_t &bits : device_depth_bits)
		{
			const float z_m = cp2077_depth::device_depth_bits_to_z_m(bits);
			if (std::isfinite(z_m))
				++valid_pixels;
			std::memcpy(&bits, &z_m, sizeof(bits));
		}

		std::ostringstream filename_builder;
		filename_builder << "depth_" << std::setfill('0') << std::setw(8)
			<< sequence << ".npy";
		const std::string filename = filename_builder.str();
		if (!write_npy_f32(depth_dir_ / filename, width, height, device_depth_bits))
		{
			++dropped_writer;
			return;
		}

		index_ << "{\"type\":\"sample\",\"seq\":" << sequence
			<< ",\"t_unix_ms\":" << timestamp_ms
			<< ",\"file\":\"depth/" << filename << "\""
			<< ",\"width\":" << width << ",\"height\":" << height
			<< ",\"valid_pixels\":" << valid_pixels << "}\n";
		index_.flush();
		++written_samples_;
	}

	std::atomic<uint64_t> dropped_gpu = 0;
	std::atomic<uint64_t> dropped_writer = 0;

private:
	std::string session_id_;
	fs::path session_dir_;
	fs::path depth_dir_;
	std::ofstream index_;
	bool valid_ = false;
	uint64_t next_sequence_ = 0;
	std::atomic<uint64_t> written_samples_ = 0;
};

struct DepthJob
{
	std::shared_ptr<SessionOutput> session;
	uint64_t sequence = 0;
	int64_t timestamp_ms = 0;
	uint32_t width = 0;
	uint32_t height = 0;
	std::vector<uint32_t> device_depth_bits;
};

class DepthWriter
{
public:
	DepthWriter() : thread_([this] { run(); }) {}
	~DepthWriter() { stop(); }

	bool enqueue(DepthJob job)
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (stopping_ || queue_.size() >= kWriterQueueLimit)
			return false;
		queue_.push_back(std::move(job));
		condition_.notify_one();
		return true;
	}

	void stop()
	{
		{
			std::lock_guard<std::mutex> lock(mutex_);
			if (stopping_)
				return;
			stopping_ = true;
		}
		condition_.notify_all();
		if (thread_.joinable())
			thread_.join();
	}

private:
	void run()
	{
		for (;;)
		{
			DepthJob job;
			{
				std::unique_lock<std::mutex> lock(mutex_);
				condition_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
				if (queue_.empty())
				{
					if (stopping_)
						return;
					continue;
				}
				job = std::move(queue_.front());
				queue_.pop_front();
			}
			job.session->write_sample(
				job.sequence, job.timestamp_ms, job.width, job.height,
				std::move(job.device_depth_bits));
		}
	}

	std::mutex mutex_;
	std::condition_variable condition_;
	std::deque<DepthJob> queue_;
	bool stopping_ = false;
	std::thread thread_;
};

struct ReadbackSlot
{
	resource host_resource = {};
	uint64_t fence_value = 0;
	std::shared_ptr<SessionOutput> session;
	uint64_t sequence = 0;
	int64_t timestamp_ms = 0;
	bool pending = false;
};

struct __declspec(uuid("1791ee71-77c5-4d9c-8be8-9100ea16f67a")) RuntimeState
{
	DepthWriter writer;
	std::shared_ptr<SessionOutput> active_session;
	std::string active_key;
	std::string rejected_key;
	double sample_hz = 5.0;
	std::chrono::steady_clock::time_point last_control_poll = {};
	std::chrono::steady_clock::time_point next_sample = {};
	effect_technique technique = {};
	effect_texture_variable texture = {};
	bool technique_enabled = false;
	bool missing_effect_reported = false;
	fence copy_fence = {};
	uint64_t next_fence_value = 1;
	std::array<ReadbackSlot, kReadbackSlots> slots = {};
	uint32_t width = 0;
	uint32_t height = 0;
};

void refresh_effect_handles(effect_runtime *runtime, RuntimeState &state)
{
	if (state.technique == 0)
		state.technique = runtime->find_technique(nullptr, kTechniqueName);
	if (state.texture == 0)
		state.texture = runtime->find_texture_variable(nullptr, kTextureName);
	if (state.technique != 0 && state.texture != 0)
	{
		state.missing_effect_reported = false;
		return;
	}
	if (!state.missing_effect_reported)
	{
		log_message(
			reshade::log::level::warning,
			"CP2077 Depth: CP2077Depth.fx is not loaded yet; depth capture is waiting.");
		state.missing_effect_reported = true;
	}
}

void set_technique_enabled(effect_runtime *runtime, RuntimeState &state, const bool enabled)
{
	refresh_effect_handles(runtime, state);
	if (state.technique == 0 || state.technique_enabled == enabled)
		return;
	runtime->set_technique_state(state.technique, enabled);
	state.technique_enabled = enabled;
}

bool process_oldest_readback(effect_runtime *runtime, RuntimeState &state, const bool block)
{
	ReadbackSlot *oldest = nullptr;
	for (ReadbackSlot &slot : state.slots)
	{
		if (slot.pending && (oldest == nullptr || slot.fence_value < oldest->fence_value))
			oldest = &slot;
	}
	if (oldest == nullptr)
		return false;

	device *const device = runtime->get_device();
	if (!device->wait(
		state.copy_fence, oldest->fence_value,
		block ? std::numeric_limits<uint64_t>::max() : 0))
		return false;

	subresource_data mapped = {};
	if (!device->map_texture_region(
		oldest->host_resource, 0, nullptr, map_access::read_only, &mapped))
	{
		++oldest->session->dropped_gpu;
		oldest->session.reset();
		oldest->pending = false;
		return true;
	}

	DepthJob job;
	job.session = oldest->session;
	job.sequence = oldest->sequence;
	job.timestamp_ms = oldest->timestamp_ms;
	job.width = state.width;
	job.height = state.height;
	job.device_depth_bits.resize(static_cast<size_t>(state.width) * state.height);
	for (uint32_t y = 0; y < state.height; ++y)
	{
		std::memcpy(
			job.device_depth_bits.data() + static_cast<size_t>(y) * state.width,
			static_cast<const uint8_t *>(mapped.data) + static_cast<size_t>(y) * mapped.row_pitch,
			static_cast<size_t>(state.width) * sizeof(uint32_t));
	}
	device->unmap_texture_region(oldest->host_resource, 0);

	const std::shared_ptr<SessionOutput> job_session = job.session;
	if (!state.writer.enqueue(std::move(job)))
		++job_session->dropped_writer;
	oldest->session.reset();
	oldest->pending = false;
	return true;
}

void drain_readbacks(effect_runtime *runtime, RuntimeState &state, const bool block)
{
	while (process_oldest_readback(runtime, state, block))
	{
	}
}

void destroy_readback_resources(effect_runtime *runtime, RuntimeState &state)
{
	device *const device = runtime->get_device();
	for (ReadbackSlot &slot : state.slots)
	{
		if (slot.host_resource != 0)
			device->destroy_resource(slot.host_resource);
		slot = {};
	}
	state.width = 0;
	state.height = 0;
}

bool ensure_readback_resources(
	effect_runtime *runtime,
	RuntimeState &state,
	const resource_desc &source_desc)
{
	const uint32_t width = source_desc.texture.width;
	const uint32_t height = source_desc.texture.height;
	if (state.width == width && state.height == height && state.slots[0].host_resource != 0)
		return true;

	command_queue *const queue = runtime->get_command_queue();
	queue->wait_idle();
	drain_readbacks(runtime, state, true);
	destroy_readback_resources(runtime, state);

	resource_desc host_desc = source_desc;
	host_desc.heap = memory_heap::gpu_to_cpu;
	host_desc.usage = resource_usage::copy_dest;
	host_desc.flags = resource_flags::none;
	device *const device = runtime->get_device();
	for (ReadbackSlot &slot : state.slots)
	{
		if (!device->create_resource(
			host_desc, nullptr, resource_usage::copy_dest, &slot.host_resource))
		{
			destroy_readback_resources(runtime, state);
			log_message(reshade::log::level::error, "CP2077 Depth: failed to create readback texture ring.");
			return false;
		}
	}
	state.width = width;
	state.height = height;
	return true;
}

void stop_active_session(RuntimeState &state)
{
	if (!state.active_session)
		return;
	log_message(reshade::log::level::info, "CP2077 Depth: recording session stopped.");
	state.active_session.reset();
	state.active_key.clear();
}

void poll_control(effect_runtime *runtime, RuntimeState &state)
{
	const auto now = std::chrono::steady_clock::now();
	if (state.last_control_poll.time_since_epoch().count() != 0 &&
		now - state.last_control_poll < kControlPollPeriod)
		return;
	state.last_control_poll = now;

	const auto control = read_control();
	if (!control || control->status != "recording" ||
		control->session_id.empty() || control->session_dir.empty())
	{
		stop_active_session(state);
		state.rejected_key.clear();
		set_technique_enabled(runtime, state, false);
		return;
	}

	const std::string key = control->session_id + "|" + control->session_dir;
	if (state.active_session && state.active_key == key)
	{
		state.sample_hz = control->sample_hz;
		return;
	}
	stop_active_session(state);
	if (state.rejected_key == key)
		return;

	auto session = std::make_shared<SessionOutput>(
		control->session_id, fs::u8path(control->session_dir), control->sample_hz);
	if (!session->valid())
	{
		state.rejected_key = key;
		log_message(
			reshade::log::level::error,
			"CP2077 Depth: cannot create depth output in recorder session directory.");
		return;
	}
	state.active_session = std::move(session);
	state.active_key = key;
	state.rejected_key.clear();
	state.sample_hz = control->sample_hz;
	state.next_sample = now;
	set_technique_enabled(runtime, state, true);
	log_message(
		reshade::log::level::info,
		"CP2077 Depth: recording session started at " +
		std::to_string(state.sample_hz) + " Hz.");
}

void on_init_effect_runtime(effect_runtime *runtime)
{
	RuntimeState &state = *runtime->create_private_data<RuntimeState>();
	if (!runtime->get_device()->create_fence(
		0, fence_flags::none, &state.copy_fence))
	{
		log_message(reshade::log::level::error, "CP2077 Depth: failed to create GPU copy fence.");
	}
}

void on_destroy_effect_runtime(effect_runtime *runtime)
{
	RuntimeState &state = *runtime->get_private_data<RuntimeState>();
	set_technique_enabled(runtime, state, false);
	stop_active_session(state);
	runtime->get_command_queue()->wait_idle();
	drain_readbacks(runtime, state, true);
	destroy_readback_resources(runtime, state);
	state.writer.stop();
	if (state.copy_fence != 0)
		runtime->get_device()->destroy_fence(state.copy_fence);
	runtime->destroy_private_data<RuntimeState>();
}

void on_reloaded_effects(effect_runtime *runtime)
{
	RuntimeState &state = *runtime->get_private_data<RuntimeState>();
	state.technique = {};
	state.texture = {};
	state.technique_enabled = false;
	refresh_effect_handles(runtime, state);
	set_technique_enabled(runtime, state, state.active_session != nullptr);
}

void on_reshade_present(effect_runtime *runtime)
{
	RuntimeState &state = *runtime->get_private_data<RuntimeState>();
	poll_control(runtime, state);
	drain_readbacks(runtime, state, false);
	set_technique_enabled(runtime, state, state.active_session != nullptr);
}

void on_reshade_finish_effects(
	effect_runtime *runtime, command_list *, resource_view, resource_view)
{
	RuntimeState &state = *runtime->get_private_data<RuntimeState>();
	if (!state.active_session || state.texture == 0 || state.copy_fence == 0)
		return;

	const auto now = std::chrono::steady_clock::now();
	if (now < state.next_sample)
		return;
	state.next_sample = now + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
		std::chrono::duration<double>(1.0 / state.sample_hz));
	drain_readbacks(runtime, state, false);

	resource_view source_view = {};
	resource_view source_view_srgb = {};
	runtime->get_texture_binding(state.texture, &source_view, &source_view_srgb);
	if (source_view == 0)
	{
		++state.active_session->dropped_gpu;
		return;
	}
	device *const device = runtime->get_device();
	const resource source = device->get_resource_from_view(source_view);
	if (source == 0)
	{
		++state.active_session->dropped_gpu;
		return;
	}
	const resource_desc source_desc = device->get_resource_desc(source);
	if (source_desc.texture.format != format::r32_float ||
		!ensure_readback_resources(runtime, state, source_desc))
	{
		++state.active_session->dropped_gpu;
		return;
	}

	ReadbackSlot *free_slot = nullptr;
	for (ReadbackSlot &slot : state.slots)
	{
		if (!slot.pending)
		{
			free_slot = &slot;
			break;
		}
	}
	if (free_slot == nullptr)
	{
		++state.active_session->dropped_gpu;
		return;
	}

	command_queue *const queue = runtime->get_command_queue();
	command_list *const commands = queue->get_immediate_command_list();
	commands->barrier(source, resource_usage::shader_resource, resource_usage::copy_source);
	commands->copy_texture_region(
		source, 0, nullptr, free_slot->host_resource, 0, nullptr);
	commands->barrier(source, resource_usage::copy_source, resource_usage::shader_resource);
	queue->flush_immediate_command_list();

	const uint64_t fence_value = state.next_fence_value++;
	if (!queue->signal(state.copy_fence, fence_value))
	{
		++state.active_session->dropped_gpu;
		return;
	}
	free_slot->fence_value = fence_value;
	free_slot->session = state.active_session;
	free_slot->sequence = state.active_session->allocate_sequence();
	free_slot->timestamp_ms = unix_ms_now();
	free_slot->pending = true;
}
} // namespace

extern "C" __declspec(dllexport) const char *NAME = "CP2077 Camera Z-Depth";
extern "C" __declspec(dllexport) const char *DESCRIPTION =
	"Asynchronously exports Cyberpunk 2077 camera Z-depth in metres while game-recorder is active.";

extern "C" __declspec(dllexport) bool AddonInit(HMODULE addon_module, HMODULE reshade_module)
{
	if (!reshade::register_addon(addon_module, reshade_module))
		return false;

	wchar_t module_path[MAX_PATH] = {};
	const DWORD length = GetModuleFileNameW(addon_module, module_path, MAX_PATH);
	if (length == 0 || length == MAX_PATH)
	{
		reshade::unregister_addon(addon_module, reshade_module);
		return false;
	}
	g_control_path = fs::path(module_path).parent_path() /
		"plugins" / "cyber_engine_tweaks" / "mods" /
		"CameraFrameLogger" / "active_session.json";

	reshade::register_event<reshade::addon_event::init_effect_runtime>(on_init_effect_runtime);
	reshade::register_event<reshade::addon_event::destroy_effect_runtime>(on_destroy_effect_runtime);
	reshade::register_event<reshade::addon_event::reshade_reloaded_effects>(on_reloaded_effects);
	reshade::register_event<reshade::addon_event::reshade_present>(on_reshade_present);
	reshade::register_event<reshade::addon_event::reshade_finish_effects>(on_reshade_finish_effects);
	return true;
}

extern "C" __declspec(dllexport) void AddonUninit(HMODULE addon_module, HMODULE reshade_module)
{
	reshade::unregister_addon(addon_module, reshade_module);
}
