"""Проверяет границы автоматического удаления входных копий."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from src.dashboard.config import DashboardConfig, INPUT_RETENTION_KEY
from src.dashboard.jobs import Jobs
from src.files.run_storage import write_json


class DashboardRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='dashboard-retention-', dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = DashboardConfig(self.root / '.env')
        self.jobs = Jobs(self.root / 'runs', self.config)

    def create_run(self, number, status, age_hours):
        run_id = f'20261108-080000-{number:012x}-web'
        work = self.jobs.metadata / run_id
        for directory in (work / '0', work / 'evaluation' / 'packages' / 'TEST'):
            directory.mkdir(parents=True)
            (directory / 'document.txt').write_text('temporary copy', encoding='utf-8')
        (work / 'questions.json').write_text('[]', encoding='utf-8')
        (work / 'keep.txt').write_text('unrelated', encoding='utf-8')
        job = work / 'job.json'
        write_json(job, {'status': status, 'label': f'Run {number}', 'created_at': '2026-11-01'})
        age = time.time() - age_hours * 3600
        os.utime(job, (age, age))
        return work

    def test_expired_finished_inputs_are_removed_and_history_is_preserved(self):
        expired = [self.create_run(1, 'completed', 26), self.create_run(2, 'failed', 26),
                   self.create_run(3, 'completed_with_errors', 26)]
        protected = [self.create_run(4, 'running', 200), self.create_run(5, 'completed', 1),
                     self.create_run(6, 'unknown', 200)]
        output = self.jobs.root / expired[0].name
        output.mkdir()
        for name in ('report.html', 'answers.json', 'inputs.json'):
            (output / name).write_text('result', encoding='utf-8')
        originals = self.root / 'test-data' / 'My test' / 'documents'
        originals.mkdir(parents=True)
        original = originals / 'document.txt'
        original.write_text('original', encoding='utf-8')

        self.jobs.cleanup_inputs(24)
        self.jobs.cleanup_inputs(24)

        for work in expired:
            self.assertEqual({path.name for path in work.iterdir()}, {'job.json', 'keep.txt'})
        for work in protected:
            self.assertTrue((work / '0' / 'document.txt').is_file())
            self.assertTrue((work / 'evaluation').is_dir())
        self.assertEqual(len(self.jobs.list()), 6)
        self.assertEqual({path.name for path in output.iterdir()}, {'report.html', 'answers.json', 'inputs.json'})
        self.assertEqual(original.read_text(encoding='utf-8'), 'original')

    def test_retention_defaults_env_override_and_disable(self):
        work = self.create_run(1, 'completed', 180)
        with patch.dict(os.environ, clear=True):
            self.assertEqual(self.config.input_retention_hours(), 24)
            self.config.path.write_text(f'{INPUT_RETENTION_KEY}=168\n', encoding='utf-8')
            self.assertEqual(self.config.input_retention_hours(), 168)
            with patch.dict(os.environ, {INPUT_RETENTION_KEY: '0'}):
                self.jobs.cleanup_inputs(self.config.input_retention_hours())
            self.assertTrue((work / '0').is_dir())
            with patch.dict(os.environ, {INPUT_RETENTION_KEY: '-1'}), self.assertRaises(ValueError):
                self.config.input_retention_hours()
            self.jobs.cleanup_inputs(self.config.input_retention_hours())
            self.assertFalse((work / '0').exists())
