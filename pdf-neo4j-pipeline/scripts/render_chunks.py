#!/usr/bin/env python3
import json
import argparse
import fitz

def chunk_to_html(chunk):
    chunk_id = chunk.get("chunk_id", "unknown_id")
    chunk_type = chunk.get("chunk_type", "unknown_type")
    label = chunk.get("display_name") or chunk.get("legal_citation", "Chunk")
    
    html = []
    html.append(f"<div style='margin-bottom: 16px; font-family: sans-serif;'>")
    html.append(f"<div style='background-color: #f0f0f0; padding: 4px; margin-bottom: 8px; font-size: 0.9em; font-weight: bold;'>")
    html.append(f"{label} (Type: {chunk_type}, ID: {chunk_id})")
    html.append(f"</div>")
    
    if chunk_type == "table_rows":
        columns = chunk.get("columns", [])
        rows = chunk.get("rows", [])
        
        html.append("<table cellpadding='4' cellspacing='0' style='width: 100%; font-size: 0.9em;'>")
        if columns:
            html.append("<tr>")
            for c in columns:
                html.append(f"<th style='background-color: #e0e0e0; text-align: left; border: 1px solid #aaa;'>{c}</th>")
            html.append("</tr>")
            
        for r in rows:
            html.append("<tr>")
            if isinstance(r, dict):
                # Row is a dictionary mapping column names to cell values
                for c in columns:
                    cell = r.get(c, "")
                    html.append(f"<td style='border: 1px solid #aaa;'>{cell}</td>")
            elif isinstance(r, list):
                # Row is a list of cell values
                for cell in r:
                    html.append(f"<td style='border: 1px solid #aaa;'>{cell}</td>")
                # Pad if fewer cells than columns
                if len(r) < len(columns):
                    for _ in range(len(columns) - len(r)):
                        html.append("<td style='border: 1px solid #aaa;'></td>")
            else:
                # Fallback to single string spanning all columns
                colspan = max(len(columns), 1)
                html.append(f"<td colspan='{colspan}' style='border: 1px solid #aaa;'>{r}</td>")
            html.append("</tr>")
        html.append("</table>")
    else:
        text = chunk.get("text", "")
        # replace newlines with br
        text_html = text.replace("\n", "<br>")
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
    html_lines.append(f"<h1 style='font-family: sans-serif;'>{title}</h1>")
    
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
