import unittest

from directory import crawlers


class McpToolSummarizationTests(unittest.TestCase):
    def test_keeps_one_hundred_valid_capabilities_within_the_cap(self):
        tools = [None, {"description": "missing a name"}]
        tools.extend(
            {
                "name": f"tool-{index}",
                "description": "x" * (crawlers._MAX_TOOL_DESC + 20),
            }
            for index in range(105)
        )

        summarized = crawlers._summarize_tools(tools)

        self.assertEqual(crawlers._MAX_TOOLS, 100)
        self.assertEqual(len(summarized), 100)
        self.assertEqual(summarized[0]["name"], "tool-0")
        self.assertEqual(summarized[-1]["name"], "tool-99")
        self.assertEqual(
            summarized[0]["description"],
            "x" * crawlers._MAX_TOOL_DESC + "…",
        )


if __name__ == "__main__":
    unittest.main()
