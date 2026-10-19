import json  
import os  
import re  
import html
from bs4 import BeautifulSoup

class Parser:
    def __init__(self):
        self=self
        
    def clean_value(self,val):  
        """Convert Jira objects/lists into readable strings."""  
        if val is None:  
            return "N/A"  
        if isinstance(val, dict):  
            return val.get('displayName') or val.get('name') or val.get('value') or str(val)  
        if isinstance(val, list):  
            if len(val) > 0 and isinstance(val[0], dict):  
                return ", ".join([item.get('displayName') or item.get('name') or str(item) for item in val])  
            return ", ".join([str(item) for item in val])  
        return str(val)
    
    def deep_clean_text(self, text):  
        """Remove HTML tags, normalize tables to Markdown, convert entities, and trim excess whitespace."""  
        if not text:  
            return ""

        # 1️⃣ HTML entities  
        text = html.unescape(text)

        # Use BeautifulSoup for structured parsing (Tables and Tags)  
        soup = BeautifulSoup(text, 'html.parser')

        # 2️⃣ Normalize Tables to Markdown  
        for table in soup.find_all('table'):  
            md_table = []  
            rows = table.find_all('tr')  
            
            for i, row in enumerate(rows):  
                # Extract cells (th or td)  
                cols = [cell.get_text(strip=True) for cell in row.find_all(['td', 'th'])]  
                if not cols:  
                    continue  
                    
                # Format row as Markdown pipe table  
                md_row = "| " + " | ".join(cols) + " |"  
                md_table.append(md_row)  
                
                # Add the separator line after the header (first row)  
                if i == 0:  
                    separator = "| " + " | ".join(['---'] * len(cols)) + " |"  
                    md_table.append(separator)  
            
            # Replace the HTML table element with the Markdown string  
            table.replace_with("\n\n" + "\n".join(md_table) + "\n\n")

        # 3️⃣ Handle Images (Keep them as tags)  
        # We protect the <img> tags by replacing them with a placeholder before soup.get_text()  
        img_placeholder = "###IMG_PLACEHOLDER###"  
        img_tags = []  
        for img in soup.find_all('img'):  
            src = img.get('src', '')  
            img_tags.append(src)  
            img.replace_with(img_placeholder)

        # 4️⃣ Strip all remaining tags and get text  
        # This effectively removes all HTML while keeping our placeholders and normalized tables  
        text = soup.get_text()

        # Restore image placeholders  
        for src in img_tags:  
            text = text.replace(img_placeholder, f'<img src="{src}" />', 1)

        # 5️⃣ Collapse blank lines (Max 2 consecutive newlines for MD readability)  
        text = re.sub(r'\n\s*\n', '\n\n', text)  
        
        # 6️⃣ Trim  
        return text.strip()

    def extract_img_src(self,text):  
        """Return a list of all image URLs found in <img src=...> tags."""  
        if not text:  
            return []  
        # Ensure we work on the raw HTML (before it is stripped)  
        raw = html.unescape(text)  
        return re.findall(r'<img\s+[^>]*src=["\']([^"\']+)["\']', raw, flags=re.IGNORECASE)

    def parse_jira_json(self,output_dir,issues):  

        # Ensure output directory exists  
        os.makedirs(output_dir+"/JiraTickets", exist_ok=True)

        for idx, issue in enumerate(issues, 1):  
            key = issue.get('key', f'UnknownKey{idx}')  
            summary = issue.get('fields', {}).get('summary', 'No Summary')
                
            # print(key)
            md_path = os.path.join(output_dir+"/JiraTickets", f"{key}.md")  
            with open(md_path, 'w', encoding='utf-8') as md:  
                md.write(f"## {idx}. {key}: {summary}\n")
                # ---------- Basic Information ----------  
                md.write("\n### 📋 Basic Information\n")  
                md.write("| Field | Value |\n")  
                md.write("| :--- | :--- |\n")
                fields = issue.get('fields', {})  
                md.write(f"| **Status** | {self.clean_value(fields.get('status'))} |\n")  
                md.write(f"| **Assignee** | {self.clean_value(fields.get('assignee'))} |\n")  
                md.write(f"| **Reporter** | {self.clean_value(fields.get('reporter'))} |\n")  
                md.write(f"| **Priority** | {self.clean_value(fields.get('priority'))} |\n")  
                md.write(f"| **Created** | {fields.get('created', 'N/A')} |\n")  
                md.write(f"| **Updated** | {fields.get('updated', 'N/A')} |\n")  
                md.write("\n")
                # ---------- Description ----------  
                description = fields.get('description')  
                md.write("### 📝 Description\n")  
                if description:  
                    # Extract images first  
                    img_urls = self.extract_img_src(description)  
                    clean_desc = self.deep_clean_text(description)  
                    md.write(f"{clean_desc}\n\n")
                    if img_urls:  
                        md.write("*Image links referenced in the description:*\n")  
                        for url in img_urls:  
                            md.write(f"- {url}\n")  
                        md.write("\n")  
                else:  
                    md.write("*No description provided.*\n\n")
                # ---------- Comments ----------  
                comments_data = fields.get('comment', {})  
                comments = comments_data.get('comments', [])  
                if comments:  
                    md.write("### 💬 Comments\n")  
                    for c in comments:  
                        author = self.clean_value(c.get('author'))  
                        timestamp = c.get('created', 'Unknown Date')  
                        body = c.get('body', '')
                        # Extract images from comment body  
                        img_urls = self.extract_img_src(body)  
                        clean_body = self.deep_clean_text(body)
                        md.write(f"**{author}** *[{timestamp}]*: {clean_body}\n\n")  
                        if img_urls:  
                            md.write("  *Images in this comment:*\n")  
                            for url in img_urls:  
                                md.write(f"  - {url}\n")  
                            md.write("\n")  
                else:  
                    md.write("*No comments.*\n")
                md.write("\n---\n\n")
            print("File " + md_path + " Saved Successfully !!")
            break

