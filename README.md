# zxart-original-downloader

抓取 [zxart.ee](https://zxart.ee) 上音乐作品的**原始 tracker 模块文件**（`.pt3` / `.pt2` / `.psc` / `.scl` / `.ay` 等），而不是网站上播放用的 ogg/mp3 音频流。

2024 年以后 zxart.ee 改版成 Angular SPA，原来作者页上的「一键下载音乐包」按钮和打包 zip 接口都下线了。但**原始文件本身并没有被删**——它只是从页面按钮挪到了前端加载列表时调用的那个 JSON 接口的 `originalFileUrl` 字段里。本仓库记录了这个方法，并提供一个可直接使用的下载脚本。

## 快速开始

```bash
# 下载作者 37405 (Mast) 的全部原始音乐文件，落到 ./mast/
python zxart_download.py 37405

# 自己指定作者目录名
python zxart_download.py 37405 -o Mast_music

# 只下某种格式
python zxart_download.py 37405 --formats pt3 pt2

# 只要清单不要文件（先看看有什么）
python zxart_download.py 37405 --dry-run
```

输出结构按「作者 / 年份」两级，不按格式分目录，文件名里也不带 tune id：

```
mast/
├── 1995/  Mast_-_ET-POP_(1995).pt2
├── 1998/  Mast_-_Amiga#3_(1998).pt3
├── 1999/  Mast_-_Aerodrome_(1999)_(DiHalt_1999,_2).pt3
├── unknown/  Mast_-_17_instants_of_spring.pt3
├── index.json
└── index.md
```

- **作者目录**默认取作者页标题的 slug（`Mast` → `mast`），`-o` 可以覆盖。不是 `author_37405` 这种带编号的名字。
- **年份目录**来自 `year` 字段；zxart 上有约 45% 的曲子 `year` 为 `null`，这些进 `unknown/`。
- **格式混放在同一层**，靠扩展名区分，`.pt3` / `.pt2` / `.psc` 混在同一个年份目录里。
- **文件名**就是服务器返回的 `filename:` 段，即原作者上传时的原始文件名，只做 Windows 非法字符替换。
- **tune id 只出现在 `index.md` 和 `index.json` 里**，不参与目录和文件名。

仅依赖 Python 3.8+ 标准库，没有第三方包。

同名文件的处理：Mast 有两人次上传用了同一个文件名（两首不同的 `Mast_-_Insomnia_(1999).pt3`、两首不同的 `Mast_-_Mad_Rave.pt2`）。因为文件名里不放 id，这类重复改用字母后缀区分：`Mast_-_Insomnia_(1999) (a).pt3` / `(b)`，按 id 升序分配，哪个是哪个查 `index.json`。

## 核心原理

### 1. 页面是 SPA，HTML 里没有数据

```bash
curl https://zxart.ee/author/37405/music
```

返回的只有 1.3 KB 的空壳，外加 `<app-root>`：

```html
<script src="/js/ng-zxart/runtime.612deb16637b8ebc.js" type="module"></script>
<script src="/js/ng-zxart/main.c544a8373b5602ef.js" type="module"></script>
```

所以凭 curl 抓 HTML 是拿不到任何曲目信息的。

### 2. 真正的接口藏在按需加载的 chunk 里

从 `runtime.js` 里能拿到 webpack 的 chunk 哈希映射表，再顺着路由找到作者页对应的 chunk（`3118.*.js` → `AuthorPageComponent`），里面的 `TunesService` 明确写着请求参数：

```js
getTunesPaged(e, a, o, c, f, x = "") {
  let b = new URLSearchParams()
    .set("action", "tunesByElement")
    .set("elementId", String(e))
    .set("start", String(a))
    .set("limit", String(o))
    .set("sortColumn", c)
    .set("sortDir", f);
  ...
  return this.http.get("/tunes/", { params: b })
}
```

### 3. 曲目接口

```
GET https://zxart.ee/tunes/?action=tunesByElement&elementId=<AUTHOR_ID>&start=0&limit=1000
```

响应（截取一首）：

```json
{
  "items": [
    {
      "id": 44811,
      "title": "Insomnia",
      "authors": [{ "name": "Mast", "url": "/author/37405" }],
      "format": "PT3",
      "year": "1999",
      "isPlayable": true,
      "mp3Url": "https://music.zxart.ee/music/44811_Mast_Insomnia.ogg",
      "originalFileUrl": "https://zxart.ee/file/id:44811/filename:Mast_-_Insomnia_%281999%29.pt3",
      "trackerFileUrl": null
    }
  ],
  "total": 222,
  "availableFormatGroups": ["ay"]
}
```

三个 url 字段的区别，这是整件事的关键：

| 字段 | 内容 | 用途 |
| --- | --- | --- |
| `mp3Url` | `music.zxart.ee/music/<id>_<author>_<title>.ogg` | 网站播放器用的转码音频流，**有损，不是原始文件** |
| `originalFileUrl` | `zxart.ee/file/id:<id>/filename:<原文件名>` | **原作者上传的原始模块文件，就是你要的** |
| `trackerFileUrl` | 通常是 `null` | 少数作品额外提供的 tracker 工程文件 |

### 4. 原始文件直接可下

```bash
curl -OJ "https://zxart.ee/file/id:44811/filename:Mast_-_Insomnia_%281999%29.pt3"
```

响应头：

```
HTTP/1.1 200 OK
Content-Disposition: attachment; filename="Mast - Insomnia (1999).pt3"
Content-Length: 7064
Content-Type: application/octet-stream
```

没有鉴权、没有 referer 校验、没有 cookie、没有速率限制。`filename` 参数其实只是个提示，真正的文件名以 `Content-Disposition` 为准（需要 `urllib.parse.unquote` 解码，因为路径里是百分号编码的）。

文件头校验：`.pt3` 应以 `ProTracker 3.` 开头。`.pt2`（ProTracker 2）没有文本魔数，开头直接是样本数据块，属正常现象，不要误判为损坏。

### 4.1 一个坑：zxart.ee 的 TLS 会随机断连

用 Python 的 `urllib.request.urlopen` 抓这个站点，大约五次里有一次直接抛：

```
ssl.SSLEOFError: EOF occurred in violation of protocol (_ssl.c:1144)
```

同样 URL 用 `curl` 或者裸的 `http.client.HTTPSConnection` 却几乎不出错。差别在 urllib 默认的 `HTTPConnection` 会带上一组自己的默认头并走 `http.client` 的 keep-alive 复用路径，而 zxart.ee 的 Apache/ZoneOS 在这个组合下会提前掐断 TLS 会话。`openssl s_client` 显示握手本身是正常的 TLSv1.3，所以不是协议版本或加密套件问题。

脚本里的对策有两条，缺一不可：

1. 不用 urllib，直接 `http.client.HTTPSConnection` 建连接，每次请求后 `close()`；
2. 失败重试 6 次，指数退避上限 3 秒。

改完之后 222 个文件一次跑通、零失败。如果你自己写脚本踩到这个错，别去调 `ssl` 版本或者 `OP_LEGACY_SERVER_CONNECT`，换 `http.client` 最省事。

### 5. 曾经的一键打包接口（已失效）

改版前的接口大约长这样，现在全部 400/404：

| 试探路径 | 结果 |
| --- | --- |
| `/tunes/?action=zip&elementId=37405` | `400` `{"responseStatus":"fail"}` |
| `/file/zip/id:37405` | `404` |
| `/tunes/zip/?elementId=37405` | `400` |
| `/prodlist/?elementId=37405&action=zip` | `200` 但是 `{}` |

所以只能逐首抓 `originalFileUrl` 自己打包。好在一个作者通常几百首、总量不到 1 MB，脚本几秒就跑完。

## 其他可能有用的接口

都是从 SPA bundle 里挖出来的，同一个 token 结构，也许对你有用：

| 接口 | 作用 |
| --- | --- |
| `/tunes/?action=tunesByElement&elementId=<id>` | 某作者/元素下的曲目 |
| `/author-details/?id=<id>` | 作者元数据 |
| `/author-prods/?id=<id>` | 作者参与的作品（游戏、demo） |
| `/author-prods/?id=<id>&role=<role>` | 按角色过滤 |
| `/prod-details/?id=<id>` | 作品详情 |
| `/prod-screenshots/?id=<id>` | 作品截图（带 `downloadUrl`） |
| `/prodlist/?elementId=<id>&start=0&limit=50` | 作品分页列表 |
| `/pictures-data/?...` | 图片列表 |
| `/file-search/?...` | 文件搜索 |
| `/comments-data/id:<id>/?lang=en` | 评论 |

注意 `/tunes/` 和 `/prodlist/` 用的是不同的 id 语义：作者 id 直接吃 `elementId`，作品 id 也吃 `elementId`。

## 参数说明

`zxart_download.py` 的位置参数是**作者 id**。怎么拿到？

打开作者页，URL 里的数字就是：`https://zxart.ee/author/37405/music` → `37405`。

常用可选参数：

- `-o, --out DIR` — 作者目录名，默认取作者标题的 slug（`Mast` → `./mast`），取不到标题时退回 `author_<id>`
- `--formats pt3 pt2` — 只要这些格式（小写，不带点）
- `--limit N` — 最多下 N 首
- `--workers N` — 并发数，默认 6
- `--dry-run` — 只打印清单，不下载
- `--overwrite` — 覆盖已存在的文件（默认跳过，可断点续传）

## 已抓取的示例数据集

本仓库不附带任何音乐文件（版权归原作者所有）。作为验证，用本方法抓取 Mast (id 37405) 得到 **222 首 / 710,804 字节**：

- `PT3` 111 首、`PT2` 105 首、`PSC` 6 首
- 年份分布：1995–2001 共 122 首，`unknown` 100 首
- 全部带 `originalFileUrl`，`trackerFileUrl` 全为 null
- 无空文件、无重复内容、无损坏，111 个 `.pt3` 全部通过 `ProTracker 3.` 魔数校验
- 222 个文件 MD5 互不相同（两对同名上传已用 `(a)`/`(b)` 后缀区分）

## 免责

仅用于个人存档与 tracker 音乐研究。文件版权归各作者所有，请勿再分发或商用。抓取时请保持低并发，别给 zxart.ee 北大西洋的服务器添麻烦。

## License

脚本与文档以 MIT 发布。被下载的音乐文件不在此列。
