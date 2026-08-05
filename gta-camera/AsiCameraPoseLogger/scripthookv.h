#pragma once

#include <windows.h>
#include <cstdint>

// Runtime binding to ScriptHookV.dll - no SDK .lib required.
// Export names are MSVC C++ mangled (x64).

struct Vector3
{
    alignas(8) float x;
    alignas(8) float y;
    alignas(8) float z;
};

using ScriptMainFn = void (*)();

bool LoadScriptHookV();
void UnloadScriptHookVBindings();

void scriptWait(DWORD time);
void scriptRegister(HMODULE module, ScriptMainFn main);
void scriptUnregister(HMODULE module);
void nativeInit(std::uint64_t hash);
void nativePush64(std::uint64_t value);
std::uint64_t* nativeCall();

inline void WAIT(DWORD time)
{
    scriptWait(time);
}

template <typename T>
inline void nativePush(T value)
{
    std::uint64_t packed = 0;
    static_assert(sizeof(T) <= sizeof(std::uint64_t), "native arg too large");
    *reinterpret_cast<T*>(&packed) = value;
    nativePush64(packed);
}

inline void pushArgs()
{
}

template <typename T>
inline void pushArgs(T arg)
{
    nativePush(arg);
}

template <typename T, typename... Ts>
inline void pushArgs(T arg, Ts... args)
{
    nativePush(arg);
    pushArgs(args...);
}

template <typename R, typename... Ts>
inline R invoke(std::uint64_t hash, Ts... args)
{
    nativeInit(hash);
    pushArgs(args...);
    return *reinterpret_cast<R*>(nativeCall());
}
