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
    evidence = {}
    for result in results:
        for item in result.get("items", ()):
            if not isinstance(item, Mapping) or not item.get("source_id"):
                continue
            previous = evidence.get(item["source_id"])
            if previous is not None and any(previous.get(key) != item.get(key)
                    for key in ("document_id", "updated_at", "title")):
                return "本轮检索期间知识文件发生变化，请重新查询后再核对条款。"
            merged = dict(item)
            if previous is not None and item.get("format") == "pdf":
                merged["selected_pages"] = sorted(set(previous.get("selected_pages", ())) |
                                                  set(item.get("selected_pages", ())))
            evidence[item["source_id"]] = merged
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
        pages = item.get("selected_pages") if item.get("format") == "pdf" else None
        page_note = "，PDF 页码 " + "、".join(map(str, pages)) if pages else ""
        citations.append(f"{source}（{item['space']}{page_note}，来源编号 {item['source_id']}）")
    limitation = ("PDF 依据限所列页的文字层；图示、扫描文字及表格版面未核验。"
                  if any(item.get("format") == "pdf" for item in evidence.values())
                  else "附件和嵌入表格未读取。")
    return answer + "\n\n依据：" + "；".join(citations) + "。" + limitation
