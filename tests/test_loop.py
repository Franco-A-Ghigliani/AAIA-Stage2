"""First tests for the action-observation loop."""

import json
import unittest

from coding_harness.agent import parse_action, run_agent


class RecordingRuntime:
    def __init__(self):
        self.actions = []

    def execute(self, action):
        self.actions.append(action)
        return {"status": "ok", "output": "example file content"}


class LoopTest(unittest.TestCase):
    def test_untrusted_reply_limits(self):
        for reply in ['x' * 48001, '[' * 2000 + '0' + ']' * 2000]:
            with self.assertRaises(ValueError):
                parse_action(reply)

    def test_tool_observation_then_final(self):
        responses = iter(
            [
                '{"tool":"read_file","args":{"path":"orders/pricing.py"}}',
                '{"final":"Inspected pricing."}',
            ]
        )
        messages_seen = []

        def fake_model(messages):
            messages_seen.append(list(messages))
            return next(responses)

        runtime = RecordingRuntime()
        result = run_agent(fake_model, runtime, "Inspect pricing", max_turns=2)

        self.assertEqual(result["termination"], "final")
        self.assertEqual(runtime.actions[0]["tool"], "read_file")
        observation = json.loads(messages_seen[1][-1]["content"])
        self.assertEqual(observation["result"]["status"], "ok")

    def test_invalid_json_consumes_a_turn(self):
        responses = iter(["not JSON", '{"final":"Recovered."}'])
        messages_seen = []

        def fake_model(messages):
            messages_seen.append(list(messages))
            return next(responses)

        result = run_agent(
            fake_model,
            RecordingRuntime(),
            "Inspect pricing",
            max_turns=2,
        )

        self.assertEqual(result["termination"], "final")
        self.assertIn("error", messages_seen[1][-1]["content"])

    def test_turn_limit(self):
        calls = []

        def fake_model(messages):
            calls.append(1)
            return '{"tool":"list_files","args":{}}'

        result = run_agent(fake_model, RecordingRuntime(), "loop", max_turns=3)
        self.assertEqual(result["termination"], "turn_limit")
        self.assertEqual(len(calls), 3)

    def test_model_error_and_cancellation(self):
        for error, expected in [(TimeoutError(), "model_error"),
                                (ConnectionRefusedError(), "model_error"),
                                (KeyboardInterrupt(), "cancelled")]:
            def fake_model(messages, error=error):
                raise error

            result = run_agent(fake_model, RecordingRuntime(), "inspect")
            self.assertEqual(result["termination"], expected)

    def test_repeated_failed_action_is_not_executed_again(self):
        edit = '{"tool":"edit_file","args":{"path":"a.py","old":"x","new":"y"}}'
        responses = iter([edit, edit, '{"tool":"write_file","args":{"path":"b","content":"c"}}',
                          edit, '{"final":"done"}'])
        messages_seen = []

        class Runtime:
            calls = []

            def execute(self, action):
                self.calls.append(action["tool"])
                ok = action["tool"] == "write_file"
                return {"status": "ok" if ok else "error", "output": "text not found"}

        def fake_model(messages):
            messages_seen.append(list(messages))
            return next(responses)

        runtime = Runtime()
        run_agent(fake_model, runtime, "repair")
        # 2nd edit is blocked; after a successful write the same edit may run again.
        self.assertEqual(runtime.calls, ["edit_file", "write_file", "edit_file"])
        self.assertIn("repeated request", messages_seen[2][-1]["content"])

    def test_wrong_shape_is_an_error_and_events_are_emitted(self):
        responses = iter(['{"tool":"read_file"}', '[1]',
                          '{"tool":"list_files","args":{}}', '{"final":"done"}'])
        events = []
        result = run_agent(lambda _m: next(responses), RecordingRuntime(), "x",
                           emit=events.append)
        self.assertEqual(result["termination"], "final")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["invalid_response", "invalid_response", "request",
                                 "result", "final", "stop"])


if __name__ == "__main__":
    unittest.main()
