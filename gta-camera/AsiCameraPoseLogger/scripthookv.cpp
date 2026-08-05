#include "scripthookv.h"

namespace
{
HMODULE g_module = nullptr;

using ScriptWaitFn = void (*)(DWORD);
using ScriptRegisterFn = void (*)(HMODULE, ScriptMainFn);
using ScriptUnregisterFn = void (*)(HMODULE);
using NativeInitFn = void (*)(std::uint64_t);
using NativePush64Fn = void (*)(std::uint64_t);
using NativeCallFn = std::uint64_t* (*)();

ScriptWaitFn g_scriptWait = nullptr;
ScriptRegisterFn g_scriptRegister = nullptr;
ScriptUnregisterFn g_scriptUnregister = nullptr;
NativeInitFn g_nativeInit = nullptr;
NativePush64Fn g_nativePush64 = nullptr;
NativeCallFn g_nativeCall = nullptr;

FARPROC Require(const char* name)
{
    FARPROC proc = GetProcAddress(g_module, name);
    if (proc == nullptr)
    {
        OutputDebugStringA("CameraPoseLogger: missing ScriptHookV export: ");
        OutputDebugStringA(name);
        OutputDebugStringA("\n");
    }
    return proc;
}
} // namespace

bool LoadScriptHookV()
{
    if (g_module != nullptr)
    {
        return g_scriptRegister != nullptr && g_scriptWait != nullptr &&
               g_nativeInit != nullptr && g_nativePush64 != nullptr && g_nativeCall != nullptr;
    }

    g_module = GetModuleHandleW(L"ScriptHookV.dll");
    if (g_module == nullptr)
    {
        g_module = LoadLibraryW(L"ScriptHookV.dll");
    }
    if (g_module == nullptr)
    {
        return false;
    }

    g_scriptWait = reinterpret_cast<ScriptWaitFn>(Require("?scriptWait@@YAXK@Z"));
    g_scriptRegister = reinterpret_cast<ScriptRegisterFn>(
        Require("?scriptRegister@@YAXPEAUHINSTANCE__@@P6AXXZ@Z"));
    g_scriptUnregister = reinterpret_cast<ScriptUnregisterFn>(
        Require("?scriptUnregister@@YAXPEAUHINSTANCE__@@@Z"));
    g_nativeInit = reinterpret_cast<NativeInitFn>(Require("?nativeInit@@YAX_K@Z"));
    g_nativePush64 = reinterpret_cast<NativePush64Fn>(Require("?nativePush64@@YAX_K@Z"));
    g_nativeCall = reinterpret_cast<NativeCallFn>(Require("?nativeCall@@YAPEA_KXZ"));

    return g_scriptWait && g_scriptRegister && g_scriptUnregister && g_nativeInit &&
           g_nativePush64 && g_nativeCall;
}

void UnloadScriptHookVBindings()
{
    g_scriptWait = nullptr;
    g_scriptRegister = nullptr;
    g_scriptUnregister = nullptr;
    g_nativeInit = nullptr;
    g_nativePush64 = nullptr;
    g_nativeCall = nullptr;
    g_module = nullptr;
}

void scriptWait(DWORD time)
{
    g_scriptWait(time);
}

void scriptRegister(HMODULE module, ScriptMainFn main)
{
    g_scriptRegister(module, main);
}

void scriptUnregister(HMODULE module)
{
    if (g_scriptUnregister != nullptr)
    {
        g_scriptUnregister(module);
    }
}

void nativeInit(std::uint64_t hash)
{
    g_nativeInit(hash);
}

void nativePush64(std::uint64_t value)
{
    g_nativePush64(value);
}

std::uint64_t* nativeCall()
{
    return g_nativeCall();
}
