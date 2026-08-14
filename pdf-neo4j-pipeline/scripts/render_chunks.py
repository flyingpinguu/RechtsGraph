#!/usr/bin/env python3
import json
import argparse
import html
import textwrap
import fitz

def escape_html(value):
    return html.escape(str(value), quote=True)

def escape_identifier(value):
    """Escape an identifier and add deterministic breaks for PDF layout."""
    lines = textwrap.wrap(
        str(value),
        width=68,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return "<br>".join(escape_html(line) for line in lines)

def chunk_to_html(chunk):
    chunk_id = chunk.get("chunk_id", "unknown_id")
    chunk_type = chunk.get("chunk_type", "unknown_type")
    label = chunk.get("display_name") or chunk.get("legal_citation", "Chunk")
    
    html = []
    html.append(f"<div style='margin-bottom: 16px; font-family: sans-serif;'>")
    html.append(f"<div style='background-color: #f0f0f0; padding: 4px; margin-bottom: 8px; font-size: 0.9em; font-weight: bold;'>")
    html.append(
        f"{escape_html(label)} (Type: {escape_html(chunk_type)})"
        f"<br>ID: {escape_identifier(chunk_id)}"
    )
    html.append(f"</div>")
    
    if chunk_type == "table_rows":
        columns = chunk.get("columns", [])
        rows = chunk.get("rows", [])

        # Very wide legal tables are unreadable and lose cells on portrait A4.
        # Preserve their structure as freely flowing labelled fields.
        if len(columns) > 12:
            for row_number, row in enumerate(rows, start=1):
                if isinstance(row, dict):
                    values = [row.get(column, "") for column in columns]
                elif isinstance(row, list):
                    values = row
                else:
                    values = [row]
                html.append(
                    f"<div style='margin-top: 8px; font-weight: bold;'>"
                    f"Row {row_number}</div>"
                )
                for index, column in enumerate(columns):
                    value = values[index] if index < len(values) else ""
                    value_html = escape_html(value).replace("\n", "<br>")
                    html.append(
                        f"<div><b>{escape_html(column)}:</b> {value_html}</div>"
                    )
            html.append("</div>")
            return "".join(html)

        # PyMuPDF Story can truncate a long table at a page boundary. Small
        # batches preserve every row while retaining enough tabular structure
        # for corpus verification.
        for batch_start in range(0, len(rows), 8):
            batch = rows[batch_start:batch_start + 8]
            html.append("<table cellpadding='4' cellspacing='0' style='width: 100%; font-size: 0.9em;'>")
            if columns:
                html.append("<tr>")
                for c in columns:
                    html.append(f"<th style='background-color: #e0e0e0; text-align: left; border: 1px solid #aaa;'>{escape_html(c)}</th>")
                html.append("</tr>")

            for r in batch:
                html.append("<tr>")
                if isinstance(r, dict):
                    # Row is a dictionary mapping column names to cell values
                    for c in columns:
                        cell = r.get(c, "")
                        cell_html = escape_html(cell).replace("\n", "<br>")
                        html.append(f"<td style='border: 1px solid #aaa;'>{cell_html}</td>")
                elif isinstance(r, list):
                    # Row is a list of cell values
                    for cell in r:
                        cell_html = escape_html(cell).replace("\n", "<br>")
                        html.append(f"<td style='border: 1px solid #aaa;'>{cell_html}</td>")
                    # Pad if fewer cells than columns
                    if len(r) < len(columns):
                        for _ in range(len(columns) - len(r)):
                            html.append("<td style='border: 1px solid #aaa;'></td>")
                else:
                    # Fallback to single string spanning all columns
                    colspan = max(len(columns), 1)
                    row_html = escape_html(r).replace("\n", "<br>")
                    html.append(f"<td colspan='{colspan}' style='border: 1px solid #aaa;'>{row_html}</td>")
                html.append("</tr>")
            html.append("</table>")
    else:
        text = chunk.get("text", "")
        # replace newlines with br
        text_html = escape_html(text).replace("\n", "<br>")
        html.append(f"<div style='font-size: 0.95em;'>{text_html}</div>")
        
    html.append("</div>")
    return "".join(html)

def main():
    parser = argparse.ArgumentParser(description="Render JSON chunks to PDF")
    parser.add_argument("--input", required=True, help="Input raw JSON file")
    parser.add_argument("--output", required=True, help="Output PDF file")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Assuming we want to render the first document found in the raw json
    docs = data.get("documents", [])
    if not docs:
        print("No documents found in JSON.")
        return
        
    doc = docs[0]
    title = doc.get("title", "Extracted Document")
    
    # We will gather all chunks and table_rows.
    # In this pipeline, table_rows are already inside doc["chunks"].
    # Chunks are already in logical document order as appended by the extractor, so we don't sort them.
    chunks = doc.get("chunks", [])
    
    html_lines = []
    html_lines.append("<html><body>")
    html_lines.append(f"<h1 style='font-family: sans-serif;'>{escape_html(title)}</h1>")
    
    for c in chunks:
        html_lines.append(chunk_to_html(c))
        
    html_lines.append("</body></html>")
    full_html = "".join(html_lines)
    
    # Generate PDF using PyMuPDF Story and DocumentWriter
    story = fitz.Story(html=full_html)
    
    # A4 dimensions in points: 595 x 842
    # Margin of 36 points (~0.5 inch)
    page_rect = fitz.Rect(0, 0, 595, 842)
    content_rect = fitz.Rect(36, 36, 595 - 36, 842 - 36)
    
    writer = fitz.DocumentWriter(args.output)
    
    more = True
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(content_rect)
        story.draw(dev)
        writer.end_page()
        
    writer.close()
    
    print(f"Successfully generated {args.output} containing {len(chunks)} chunks.")

if __name__ == "__main__":
    main()
