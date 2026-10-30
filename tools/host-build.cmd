@echo off
setlocal
if not defined MGMEM_VS_ROOT set "MGMEM_VS_ROOT=C:\Program Files\Microsoft Visual Studio\18\Community"
call "%MGMEM_VS_ROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
set "MGMEM_CMAKE=%MGMEM_VS_ROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
set "MGMEM_NINJA=%MGMEM_VS_ROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
"%MGMEM_CMAKE%" %*
exit /b %errorlevel%
