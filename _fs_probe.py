"""Temporary diagnostic: is the page really fullscreen? What is the OS window doing?"""
import subprocess
import time

from playwright.sync_api import sync_playwright

URL = "https://ghaif1.github.io/ytshortsautomation/"

PS_INFO = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint f);
  [DllImport("user32.dll")] public static extern bool GetMonitorInfo(IntPtr h, ref MONITORINFO i);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
  public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
$p = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like '*Bouncing Balls*' } | Sort-Object StartTime -Descending | Select-Object -First 1
if ($null -eq $p) { Write-Output "NOT_FOUND"; exit }
$h = $p.MainWindowHandle
$r = New-Object W32+RECT
[W32]::GetWindowRect($h, [ref]$r) | Out-Null
Write-Output ("iconic=" + [W32]::IsIconic($h) + " zoomed(maximized)=" + [W32]::IsZoomed($h))
Write-Output ("window_rect=" + $r.Left + "," + $r.Top + " " + ($r.Right - $r.Left) + "x" + ($r.Bottom - $r.Top))
Write-Output ("screen_metrics=" + [W32]::GetSystemMetrics(0) + "x" + [W32]::GetSystemMetrics(1))
$mon = New-Object W32+MONITORINFO
$mon.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf([type][W32+MONITORINFO])
$mh = [W32]::MonitorFromWindow($h, 2)
if ([W32]::GetMonitorInfo($mh, [ref]$mon)) {
  Write-Output ("monitor_rect=" + $mon.rcMonitor.Left + "," + $mon.rcMonitor.Top + " " + ($mon.rcMonitor.Right - $mon.rcMonitor.Left) + "x" + ($mon.rcMonitor.Bottom - $mon.rcMonitor.Top))
} else { Write-Output "monitor_rect=?" }
"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel="chrome")
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("#canvas", timeout=30000)
        time.sleep(1.0)
        print("\n--- BEFORE fullscreen click ---")
        js = page.evaluate(
            "() => ({fs: Boolean(document.fullscreenElement), vs: document.visibilityState, "
            "inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight], "
            "screen: [screen.width, screen.height], "
            "box: (() => { const b = document.getElementById('canvas').getBoundingClientRect(); "
            "return [b.x, b.y, b.width, b.height]; })()})"
        )
        print("JS:", js)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_INFO],
                             capture_output=True, text=True, timeout=40)
        print("PS:", out.stdout.strip() or out.stderr.strip())

        page.click("#btn-fullscreen")
        time.sleep(2.5)
        print("\n--- AFTER fullscreen click ---")
        js = page.evaluate(
            "() => ({fs: Boolean(document.fullscreenElement), vs: document.visibilityState, "
            "inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight], "
            "screen: [screen.width, screen.height], "
            "box: (() => { const b = document.getElementById('canvas').getBoundingClientRect(); "
            "return [b.x, b.y, b.width, b.height]; })()})"
        )
        print("JS:", js)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_INFO],
                             capture_output=True, text=True, timeout=40)
        print("PS:", out.stdout.strip() or out.stderr.strip())
        browser.close()


if __name__ == "__main__":
    main()