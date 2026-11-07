"""Проверки реального входа CLI без ключа API и без сетевых запросов."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class CliEntrypointTests(unittest.TestCase):
    """Проверяет коды завершения процесса, UTF-8 и доступность справки."""

    root = Path(__file__).resolve().parents[1]

    def test_scripts_parse_in_windows_powershell_51(self):
        """Настоящий парсер 5.1 обнаруживает потерю BOM у русского текста скриптов."""
        powershell = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not powershell.is_file():
            self.skipTest('Windows PowerShell 5.1 доступен только на Windows')
        for name in ('run.ps1', 'setup.ps1'):
            path = str(self.root / name).replace("'", "''")
            command = ("$tokens=$null; $errors=$null; "
                       "[System.Management.Automation.Language.Parser]::ParseFile('" + path +
                       "', [ref]$tokens, [ref]$errors) > $null; if ($errors.Count) { exit 1 }")
            result = subprocess.run([str(powershell), '-NoProfile', '-Command', command],
                                    capture_output=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, name)

    def invoke(self, *arguments):
        environment = dict(os.environ, PYTHONIOENCODING='utf-8', NO_COLOR='1')
        environment.pop('ITFEST_API_KEY', None)
        return subprocess.run(
            [sys.executable, str(self.root / 'cli.py'), *arguments],
            cwd=self.root, env=environment, capture_output=True,
            text=True, encoding='utf-8', timeout=15, check=False)

    def test_help_process_succeeds_for_every_supported_command_without_api(self):
        for command in (None, 'query', 'evaluate', 'compare', 'report'):
            with self.subTest(command=command):
                arguments = ['--help'] if command is None else [command, '--help']
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Usage: cli.py', result.stdout)
                if command is not None:
                    self.assertIn('cli.py ' + command, result.stdout)
                self.assertNotIn('Traceback', result.stderr)
        self.assertIn('--atacment', self.invoke('query', '--help').stdout)

    def test_removed_commands_exit_before_model_configuration(self):
        for command in ('run', 'ask'):
            with self.subTest(command=command):
                result = self.invoke(command)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('ITFEST_API_KEY', result.stderr)
                self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
