#pragma once

#ifdef __cplusplus
extern "C" {
#endif

WINBASEAPI
HRESULT
WINAPI
SetThreadDescription(
    _In_ HANDLE hThread,
    _In_ PCWSTR lpThreadDescription
    );

WINBASEAPI
HRESULT
WINAPI
GetThreadDescription(
    _In_ HANDLE hThread,
    _Outptr_result_z_ PWSTR* ppszThreadDescription
    );

#ifdef __cplusplus
}
#endif
