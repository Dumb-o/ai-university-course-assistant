import sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


def scrape_links(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LinkScraper/1.0)"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        links.append(urljoin(url, a["href"]))

    return links


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <url>")
        sys.exit(1)

    try:
        found = scrape_links(sys.argv[1])
        for link in found:
            print(link)
        print(f"\nTotal links: {len(found)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
