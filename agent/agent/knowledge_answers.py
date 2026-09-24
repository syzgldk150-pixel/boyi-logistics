"""Render only citations actually returned by the bounded knowledge reader."""
from __future__ import annotations

import re
from typing import Mapping, Sequence


def cite_knowledge(answer: str, results: Sequence[Mapping]) -> str:
    if not results:
        return answer
    failures = [item for item in results if item.get("ok") is False]
    if failures:
        return "尚未取得可核验的知识依据。" + str(failures[-1].get("message") or "知识读取失败。")
    evidence = {item["source_id"]: item for result in results for item in result.get("items", ())
                if isinstance(item, Mapping) and item.get("source_id")}
    if not evidence:
        return "本次在授权知识库内未找到匹配依据，暂不能据此判断规则。"
    urls = {item.get("url") for item in evidence.values() if item.get("url")}
    answer = re.sub(r"https?://[^\s)\]>]+", lambda match: match.group(0) if match.group(0) in urls else "（链接未核实）", answer)
    citations = []
    for item in evidence.values():
        title = str(item["title"]).replace("[", "［").replace("]", "］")
        source = f"《{title}》"
        if item.get("url"):
            source = f"[{source}]({item['url']})"
        citations.append(f"{source}（{item['space']}，来源编号 {item['source_id']}）")
    return answer + "\n\n依据：" + "；".join(citations) + "。附件和嵌入表格未读取。"
