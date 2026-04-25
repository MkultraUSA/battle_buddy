from modules.transcription import transcribe
import unittest

class TestTranscribe(unittest.TestCase):
    def test_transcribe_exists(self):
        self.assertTrue(callable(transcribe))

if __name__ == '__main__':
    unittest.main()
