#pragma once

#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>

namespace cp2077_depth
{
constexpr double kDepthBitsDenominator = 4294967295.0;
constexpr double kDepthNumerator = 1.28;
constexpr double kDepthOffset = 0.000077579959;
constexpr double kDepthExponentScale = 354.9329993;
constexpr double kDepthExponentBias = -83.84035513;

inline float device_depth_bits_to_z_m(const uint32_t bits) noexcept
{
	float device_depth = 0.0f;
	std::memcpy(&device_depth, &bits, sizeof(device_depth));
	if (!std::isfinite(device_depth) || device_depth < 0.0f || device_depth > 1.0f)
		return std::numeric_limits<float>::quiet_NaN();

	const double normalized_bits = static_cast<double>(bits) / kDepthBitsDenominator;
	const double denominator = kDepthOffset +
		std::exp(kDepthExponentScale * normalized_bits + kDepthExponentBias);
	const double z_m = kDepthNumerator / denominator;
	if (!std::isfinite(z_m) || z_m <= 0.0)
		return std::numeric_limits<float>::quiet_NaN();
	return static_cast<float>(z_m);
}
} // namespace cp2077_depth
