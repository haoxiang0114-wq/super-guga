"""Small evidence-producing web search, run only in the agent worker."""
import requests,re
from html.parser import HTMLParser
class Results(HTMLParser):
    def __init__(self):super().__init__();self.results=[];self.item=None;self.depth=0;self.anchor=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='li' and 'b_algo' in a.get('class','').split():self.item=dict(title='',url='',snippet='');self.depth=1
        elif self.item is not None:
            if tag=='li':self.depth+=1
            if tag=='a' and not self.item['url'] and a.get('href','').startswith('http'):self.item['url']=a['href'];self.anchor=True
    def handle_endtag(self,tag):
        if self.item is None:return
        if tag=='a':self.anchor=False
        if tag=='li':
            self.depth-=1
            if not self.depth:
                if self.item['title'] and self.item['url']:self.results.append(self.item)
                self.item=None
    def handle_data(self,data):
        if self.item is not None:
            key='title' if self.anchor else 'snippet'
            self.item[key]+=data+' '
def _clean_title(value):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    text = re.sub(r'^\S+\.\w{2,}\s+https?://\S+\s*(?:›\s*)?', '', text)
    text = re.sub(r'^https?://\S+\s*', '', text)
    return text[:160]


def search(query, max_results=4):
    """Public snippets only; the caller treats them as untrusted material."""
    query = str(query).strip()[:300]
    if not query: raise ValueError('搜索关键词不能为空')
    response = requests.get('https://www.bing.com/search', params={'q': query, 'setlang': 'zh-hans'},
                            headers={'User-Agent': 'Mozilla/5.0'}, timeout=(5, 8))
    try:
        response.raise_for_status()
        parser = Results(); parser.feed(response.text[:2_000_000])
    finally:
        response.close()
    found, seen = [], set()
    for item in parser.results:
        url = str(item.get('url') or '').strip()
        if not url.startswith(('http://', 'https://')) or url in seen:
            continue
        seen.add(url)
        found.append(dict(title=_clean_title(item.get('title')) or url[:60], url=url,
                          snippet=re.sub(r'\s+', ' ', str(item.get('snippet') or '')).strip()[:550]))
        if len(found) >= int(max_results):
            break
    return dict(ok=bool(found), results=found,
                message='公开搜索摘要，引用来源后汇报；摘要不等于已核实全文' if found
                else '搜索页没有可读取摘要，可能为验证页；不能编造搜索结果')
