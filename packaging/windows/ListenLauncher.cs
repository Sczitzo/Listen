using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class ListenLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool checkOnly = args.Length == 1 && args[0] == "--check";
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string tools = Path.Combine(folder, ".venv", "Scripts");
        string consolePython = Path.Combine(tools, "python.exe");
        bool isolated = File.Exists(consolePython);
        string version = isolated ? "" : "-3.13 ";
        try
        {
            var probe = new ProcessStartInfo(isolated ? consolePython : "py.exe", version +
                "-c \"import struct, sys, importlib.util as u; assert sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8; assert all(u.find_spec(n) for n in ('tkinter','webview','clr','numpy','sounddevice','PIL','pynput','pystray','pyperclip','requests','anthropic','faster_whisper','ctranslate2'))\"");
            probe.WorkingDirectory = folder;
            probe.UseShellExecute = false;
            probe.CreateNoWindow = true;
            using (var check = Process.Start(probe))
            {
                if (!check.WaitForExit(10000))
                {
                    check.Kill();
                    check.WaitForExit();
                    return NeedsSetup(checkOnly);
                }
                if (check.ExitCode != 0) return NeedsSetup(checkOnly);
            }
            if (checkOnly) return 0;

            var launch = new ProcessStartInfo(isolated ? Path.Combine(tools, "pythonw.exe") : "pyw.exe",
                version + "\"" + Path.Combine(folder, "launch_listen.pyw") + "\"");
            launch.WorkingDirectory = folder;
            launch.UseShellExecute = false;
            using (Process.Start(launch)) { }
            return 0;
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return NeedsSetup(checkOnly);
        }
    }

    private static int NeedsSetup(bool silent)
    {
        if (!silent)
            MessageBox.Show("Listen needs Python 3.13 (64-bit) and its Python packages.\n\nFollow the Windows setup instructions in the Listen README before opening it again.",
                "Listen needs setup", MessageBoxButtons.OK, MessageBoxIcon.Information);
        return 1;
    }
}
