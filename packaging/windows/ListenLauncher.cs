using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class ListenLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        bool check = args.Length == 1 && args[0] == "--check";
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(folder, "python", check ? "python.exe" : "pythonw.exe");
        try
        {
            var start = new ProcessStartInfo(python,
                "-I -B \"" + Path.Combine(folder, "packaged_setup.py") + "\"" + (check ? " --check" : ""));
            start.WorkingDirectory = folder;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (var process = Process.Start(start))
            {
                if (!check) return 0;
                if (process.WaitForExit(30000)) return process.ExitCode;
                process.Kill();
                process.WaitForExit();
                return 1;
            }
        }
        catch (Win32Exception)
        {
            if (!check)
                MessageBox.Show("Listen's bundled Python could not start. Reinstall Listen and try again.",
                    "Listen setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
