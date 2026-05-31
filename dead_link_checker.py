import os
import json
import re
import argparse
import sys
import socket
import ipaddress
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Please install required packages: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

URL_REGEX = re.compile(r'https?://[^\s<>\"\'\)]+')

def extract_urls_from_html(file_path):
    urls = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')
            for a_tag in soup.find_all('a', href=True):
                url = a_tag['href']
                if url.startswith('http://') or url.startswith('https://'):
                    urls.add(url)
            for img_tag in soup.find_all('img', src=True):
                url = img_tag['src']
                if url.startswith('http://') or url.startswith('https://'):
                    urls.add(url)
    except Exception as e:
        print(f"Error reading HTML {file_path}: {e}", file=sys.stderr)
    return urls

def extract_urls_from_notebook(file_path):
    urls = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            nb = json.load(f)
            cells = nb.get('cells', [])
            for cell in cells:
                source = cell.get('source', [])
                if isinstance(source, list):
                    source = ''.join(source)
                
                # Use regex to find urls in markdown or code cells
                found = URL_REGEX.findall(source)
                urls.update(found)
    except Exception as e:
        print(f"Error reading Notebook {file_path}: {e}", file=sys.stderr)
    return urls

def extract_urls_from_text(file_path):
    urls = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            found = URL_REGEX.findall(content)
            urls.update(found)
    except Exception as e:
        print(f"Error reading Text/Markdown {file_path}: {e}", file=sys.stderr)
    return urls

def is_safe_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
            
        if hostname.lower() in ('localhost', 'metadata.google.internal', '169.254.169.254'):
            return False
            
        ip_addr = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_addr)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
        return True
    except Exception:
        return False

def check_url(url, timeout=10):
    if not is_safe_url(url):
        return url, False, None, "Unsafe URL (metadata or local IP) blocked"
        
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        # First try HEAD request for efficiency
        with requests.head(url, timeout=timeout, allow_redirects=True, headers=headers) as response:
            status_code = response.status_code
            reason = response.reason
            
        if status_code >= 400:
            # Fallback to GET if HEAD fails or gives error (some servers block HEAD)
            with requests.get(url, timeout=timeout, stream=True, headers=headers) as response:
                status_code = response.status_code
                reason = response.reason
                # Fully close the stream without downloading the body to prevent leaks
                response.close()
        
        if status_code >= 400:
            return url, False, status_code, reason
        return url, True, status_code, "OK"
    except requests.exceptions.RequestException as e:
        return url, False, None, str(e)

def process_files(target_path):
    file_urls = {} # URL -> set of files where it appears
    
    if os.path.isfile(target_path):
        files = [target_path]
    else:
        files = []
        for root, _, filenames in os.walk(target_path):
            for filename in filenames:
                files.append(os.path.join(root, filename))
                
    for fpath in files:
        urls = set()
        if fpath.endswith('.html') or fpath.endswith('.htm'):
            urls = extract_urls_from_html(fpath)
        elif fpath.endswith('.ipynb'):
            urls = extract_urls_from_notebook(fpath)
        elif fpath.endswith('.md') or fpath.endswith('.json'):
            urls = extract_urls_from_text(fpath)
            
        for u in urls:
            # Clean trailing punctuation that might be caught by regex
            u = u.rstrip('.,;:')
            if u not in file_urls:
                file_urls[u] = set()
            file_urls[u].add(fpath)
            
    return file_urls

def main():
    parser = argparse.ArgumentParser(description="Check for dead links in HTML, Jupyter Notebooks, Markdown, and JSON.")
    parser.add_argument("path", help="File or directory to scan")
    parser.add_argument("-o", "--output", help="Output JSON file for report", default="broken_links_report.json")
    parser.add_argument("-t", "--timeout", help="Timeout in seconds for each HTTP request", type=int, default=10)
    parser.add_argument("-w", "--workers", help="Number of concurrent workers", type=int, default=10)
    
    args = parser.parse_args()
    
    if not os.path.exists(args.path):
        print(f"Error: Path {args.path} does not exist.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Scanning {args.path} for URLs...", file=sys.stderr)
    file_urls = process_files(args.path)
    
    all_urls = list(file_urls.keys())
    print(f"Found {len(all_urls)} unique URLs. Checking status...", file=sys.stderr)
    
    broken_links = []
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_url = {executor.submit(check_url, url, args.timeout): url for url in all_urls}
        
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                url, is_alive, status_code, reason = future.result()
                if not is_alive:
                    broken_links.append({
                        "url": url,
                        "status_code": status_code,
                        "reason": reason,
                        "found_in": list(file_urls[url])
                    })
                    print(f"[DEAD] {url} - {status_code} {reason}", file=sys.stderr)
            except Exception as e:
                broken_links.append({
                    "url": url,
                    "status_code": None,
                    "reason": str(e),
                    "found_in": list(file_urls[url])
                })
                print(f"[ERROR] {url} - {str(e)}", file=sys.stderr)
                
    report = {
        "total_urls_checked": len(all_urls),
        "total_broken_links": len(broken_links),
        "broken_links": broken_links
    }
    
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)
        
    print(f"Done. Report saved to {args.output}", file=sys.stderr)
    if len(broken_links) > 0:
        print(f"Found {len(broken_links)} broken links.", file=sys.stderr)
        sys.exit(1) # Exit with error code if broken links found, useful for CI
    else:
        print("All links are healthy!", file=sys.stderr)
        sys.exit(0)

if __name__ == "__main__":
    main()
