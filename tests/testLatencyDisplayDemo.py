'''
Author: wilbur
Version: 1.0
Date: 2026-10-12
Description: Static DOM contract tests for the latency display confirmation demo.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


rootPath = Path(__file__).resolve().parents[1]
demoPath = rootPath / 'docs/261012_latencyDisplayDemo.html'


@dataclass
class DomNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: 'DomNode | None' = None
    children: list['DomNode'] = field(default_factory=list)
    parts: list['DomNode | str'] = field(default_factory=list)
    text: str = ''

    def classNames(self) -> set[str]:
        return set(self.attrs.get('class', '').split())

    def hasClass(self, className: str) -> bool:
        return className in self.classNames()

    def textContent(self) -> str:
        if not self.parts:
            return self.text
        return ''.join(part if isinstance(part, str) else part.textContent() for part in self.parts)


class DomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = DomNode('root')
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = DomNode(
            tag=tag,
            attrs={key: value or '' for key, value in attrs},
            parent=self.current,
        )
        self.current.children.append(node)
        self.current.parts.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag:
            self.current = self.current.parent or self.root

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent or self.root
        if node is not self.root:
            self.current = node.parent or self.root

    def handle_data(self, data: str) -> None:
        self.current.text += data
        self.current.parts.append(data)


def descendants(node: DomNode, predicate) -> list[DomNode]:
    found: list[DomNode] = []
    for child in node.children:
        if predicate(child):
            found.append(child)
        found.extend(descendants(child, predicate))
    return found


def firstByClass(root: DomNode, className: str) -> DomNode:
    matches = descendants(root, lambda node: node.hasClass(className))
    assert matches, f'missing .{className}'
    return matches[0]


def testLatencyDisplayDemoDomContract() -> None:
    parser = DomParser()
    parser.feed(demoPath.read_text(encoding='utf-8'))
    root = parser.root

    usage = firstByClass(root, 'status-usage')
    usageText = ' '.join(usage.textContent().split())
    latency = firstByClass(usage, 'latency-summary')
    latencyText = ' '.join(latency.textContent().split())
    assert usageText.index('0.3% / 1.0M') < usageText.index('TTFT 1.9s') < usageText.index('TPS 42.6 tok/s')
    assert 'Thinking' not in latencyText
    assert 'Output' not in latencyText
    assert '--' not in latencyText

    messageBody = firstByClass(root, 'msg-body')
    thinkingBlocks = descendants(messageBody, lambda node: node.hasClass('thinking-block'))
    thinkingElapsed = descendants(messageBody, lambda node: node.hasClass('thinking-elapsed'))
    assert len(thinkingBlocks) == 2
    assert [node.textContent().strip() for node in thinkingElapsed] == ['Thinking 1.5s', 'Thinking 0.8s']
    for thinkingBlock in thinkingBlocks:
        index = thinkingBlock.parent.children.index(thinkingBlock)
        assert index + 1 < len(thinkingBlock.parent.children)
        assert thinkingBlock.parent.children[index + 1].hasClass('thinking-elapsed')

    toolCard = firstByClass(root, 'tool-card')
    assert toolCard.tag == 'details'
    assert 'open' in toolCard.attrs
    toolDetail = firstByClass(toolCard, 'tool-card-detail')
    toolExecution = firstByClass(toolDetail, 'tool-execution')
    assert toolExecution.parent is toolDetail

    toolGroup = firstByClass(root, 'tool-group')
    toolCall = firstByClass(toolGroup, 'tool-call-total')
    assert toolCall.parent is toolGroup
    assert toolCall.parent is not toolCard
    assert toolCall not in descendants(toolCard, lambda node: node.hasClass('tool-call-total'))

    answer = firstByClass(root, 'answer-content')
    total = firstByClass(messageBody, 'total-elapsed')
    assert total.parent is messageBody
    assert messageBody.children.index(answer) < messageBody.children.index(total)
    assert 'Total 9.2s' == ' '.join(total.textContent().split())

    timeline = firstByClass(root, 'demo-timeline')
    timelineText = ' '.join(timeline.textContent().split())
    assert 'TTFT 1.9s' in timelineText
    assert 'Thinking 1.5s' in timelineText
    assert 'Tool call 2.9s' in timelineText
    assert 'Final output 2.1s' in timelineText
    assert 'Total 9.2s' in timelineText
    assert not descendants(root, lambda node: node.hasClass('step-elapsed'))
