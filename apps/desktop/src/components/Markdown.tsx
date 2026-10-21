import type { ReactNode } from "react";

/**
 * Minimal markdown renderer for assistant replies.
 *
 * Deliberately tiny and dependency-free: the packaged app is offline-first and
 * a full parser (react-markdown + remark) would add ~100 KB for the handful of
 * constructs models actually emit. Everything is rendered as React elements —
 * no `dangerouslySetInnerHTML`, so model output can never inject markup.
 *
 * Supported: fenced code blocks, headings (#…####), unordered / ordered lists,
 * blockquotes, horizontal rules, paragraphs, `inline code`, **bold**, *italic*,
 * [links](https://…) and single line breaks.
 */

type Block =
  | { kind: "code"; text: string; lang: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "rule" }
  | { kind: "paragraph"; lines: string[] };

const HEADING = /^(#{1,4})\s+(.*)$/;
const UL_ITEM = /^\s*[-*•]\s+(.*)$/;
const OL_ITEM = /^\s*\d+[.)]\s+(.*)$/;
const QUOTE = /^>\s?(.*)$/;
const RULE = /^\s*([-*_])\1{2,}\s*$/;
const FENCE = /^\s*```(\w*)\s*$/;
const BLOCK_START = [HEADING, UL_ITEM, OL_ITEM, QUOTE, RULE, FENCE];

function startsBlock(line: string): boolean {
  return BLOCK_START.some((pattern) => pattern.test(line));
}

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(FENCE);
    if (fence) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !FENCE.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // closing fence
      blocks.push({ kind: "code", text: body.join("\n"), lang: fence[1] ?? "" });
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      index += 1;
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length,
        text: heading[2].trim(),
      });
      index += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const quoted: string[] = [];
      while (index < lines.length && QUOTE.test(lines[index])) {
        quoted.push(lines[index].replace(QUOTE, "$1"));
        index += 1;
      }
      blocks.push({ kind: "quote", lines: quoted });
      continue;
    }

    const ordered = OL_ITEM.test(line);
    if (ordered || UL_ITEM.test(line)) {
      const pattern = ordered ? OL_ITEM : UL_ITEM;
      const items: string[] = [];
      while (index < lines.length && pattern.test(lines[index])) {
        items.push(lines[index].replace(pattern, "$1").trim());
        index += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    const paragraph: string[] = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push({ kind: "paragraph", lines: paragraph });
  }

  return blocks;
}

// A fresh RegExp per call: the parser recurses (bold inside a list item, etc.),
// and a shared /g regex would have its lastIndex reset by the inner call,
// sending the outer loop back to the beginning (infinite loop -> frozen tab).
const INLINE_SOURCE =
  "(`[^`]+`)|(\\*\\*[^*]+\\*\\*)|(__[^_]+__)|(\\*[^*]+\\*)|(\\[[^\\]]+\\]\\((?:https?:\\/\\/|\\/)[^)]+\\))";

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = new RegExp(INLINE_SOURCE, "g");
  let cursor = 0;
  let match: RegExpExecArray | null;
  let index = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const token = match[0];
    const key = `${keyPrefix}-i${index++}`;

    if (token.startsWith("`")) {
      nodes.push(
        <code className="md-code" key={key}>
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("**") || token.startsWith("__")) {
      nodes.push(
        <strong key={key}>{renderInline(token.slice(2, -2), key)}</strong>,
      );
    } else if (token.startsWith("[")) {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {renderInline(link[1], key)}
          </a>,
        );
      } else {
        nodes.push(token);
      }
    } else {
      nodes.push(<em key={key}>{renderInline(token.slice(1, -1), key)}</em>);
    }
    cursor = match.index + token.length;
  }

  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function withBreaks(lines: string[], keyPrefix: string): ReactNode[] {
  return lines.flatMap((line, index) => {
    const nodes = renderInline(line, `${keyPrefix}-l${index}`);
    return index === lines.length - 1 ? nodes : [...nodes, <br key={`${keyPrefix}-br${index}`} />];
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks = parseBlocks(text);
  return (
    <div className="md">
      {blocks.map((block, index) => {
        const key = `b${index}`;
        switch (block.kind) {
          case "code":
            return (
              <pre className="md-pre" key={key}>
                <code>{block.text}</code>
              </pre>
            );
          case "heading": {
            const Tag = (`h${Math.min(block.level + 2, 6)}`) as "h3" | "h4" | "h5" | "h6";
            return (
              <Tag className="md-heading" key={key}>
                {renderInline(block.text, key)}
              </Tag>
            );
          }
          case "list": {
            const Tag = block.ordered ? "ol" : "ul";
            return (
              <Tag className="md-list" key={key}>
                {block.items.map((item, itemIndex) => (
                  <li key={`${key}-${itemIndex}`}>{renderInline(item, `${key}-${itemIndex}`)}</li>
                ))}
              </Tag>
            );
          }
          case "quote":
            return (
              <blockquote className="md-quote" key={key}>
                {withBreaks(block.lines, key)}
              </blockquote>
            );
          case "rule":
            return <hr className="md-rule" key={key} />;
          default:
            return (
              <p className="md-paragraph" key={key}>
                {withBreaks(block.lines, key)}
              </p>
            );
        }
      })}
    </div>
  );
}
