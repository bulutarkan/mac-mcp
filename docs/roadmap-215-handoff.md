# Roadmap #215 — Ana ajana teslim

Branch: `fix/roadmap-215-extraction`
Worktree: `/Users/tarkanbulut/.codex/worktrees/1e09/mac-mcp`
Başlangıç: `968f075` (`main`in 2026-10-16 inceleme anındaki commit'i).

Bu branch ayrı worktree'de geliştirildi. Ana checkout üzerinde checkout/edit, merge, push veya runtime promotion yapılmadı. Ana ajanın daha sonra `main`e aldığı CLI dosyaları bu değişikliklerle örtüşmüyor; merge anında güncel durumu tekrar kontrol etmek gerekir.

## Sonuç

- Mevcut `browser_act` extraction şeması, gerçek `name/selector/attr/all/max_items` nesne sözleşmesini ilan eder. `browser_do` aynı action'ı kullanır; semantic `extract=['price','rating']` korunur.
- Eksik/bozuk field, bozuk CSS ve bozuk regex açık hata verir; selector hiçbir zaman kendiliğinden `body` olmaz.
- Text extraction ve scan gizli içerik, script/style/template/noscript ve gizli shadow/frame atalarını eler; `display:contents` metni korunur. Açık `attr:'html'` eski ham markup davranışını korur.
- Matched/eligible/returned sayıları, kesilme ve devam konumu açık döner. Uzun metin aynı kayıtta tam kesildiği yerden sürer; Unicode/emoji kesilmez, offset'ler tarayıcı UTF-16 birimindedir.
- `browser_do` 8 KB bütçesi altında kesilen çıktının kalanına ulaşılabilir; metadata bütçesi tek başına dolarsa açık hata verir.
- Scan tek araç çağrısı içinde çoklu scroll/read yapar; mevcut render'daki bekleyen kayıtlar tüketilmeden kaydırmaz. `next_cursor` ile item/text/byte/step/time limitlerinden sonra devam eder.
- Cursor aynı document/selector/key/container/scroll konumuna bağlıdır; navigation, araya giren scroll veya beş dakikalık expiry'de reddedilir. En çok dört oturum/10.000 kayıt; DOM kimlikleri WeakMap ile detached düğümleri tutmaz.
- Aynı metni taşıyan ayrı kayıtlar korunur. Stable id/data key bulunmayan remount'larda yinelenme olabilir; veri kaybetmek yerine kapsam korunur. `key_field='href'` özellikle linke göre dedup yapar.
- Katalog 31 core / 103 full araç olarak kaldı. Core JSON 45.517 → 46.321 byte: doğru extraction şeması yaklaşık 804 byte ekler. Kazanç görev başına gereksiz discovery/observe/retry çağrılarını azaltmaktır; araç sayısı azaltıldığı iddia edilmez.
- REST v2 OpenAPI sözleşmesi yeniden üretildi. Kullanım örnekleri `docs/browser.md` içinde.

## Doğrulama

- Başlangıç: 58 ilgili test geçti.
- Yeni regresyonlar düzeltmelerden önce başarısız çalıştırıldı.
- Gerçek headless Chrome tier: 27 test geçti. 450 statik kayıt, 80 sanallaştırılmış kayıt, 9.010 karakterlik metin; kayıt/text devamı, emoji, aynı metinli kayıtlar, CSS hatası, gizli içerik, cursor scope/scroll, child target, detached-node retention ve display:contents kapsandı.
- Bağımsız read-only review'daki yedi önemli bulgu düzeltildi; yeniden incelemede açık Important/Critical bulgu kalmadı, reviewer'ın sekiz hedefli testi geçti.
- Gerçek Safari + ayrı geçici state/ephemeral port + HTTP MCP: fixture yorumları 2 çağrıda (3 görünür/4 eşleşme), 80 virtual kayıt tek scan çağrısında, 9.010 karakterlik metin 2 çağrıda eksiksiz alındı. Ölçülen review çağrıları 2.742/414 ms; virtual scan 6.964 ms. Model düşünme süresi veya Fragrantica sayfasıyla apples-to-apples hız karşılaştırması değildir. Test sekmesi, HTTP sunucusu ve MCP sunucusu kapatıldı; production 8765 kullanılmadı.
- İlk full-suite denemesinde worktree'de shell kapalı varsayılanı nedeniyle shell testleri hata verdi; test ortamında `MCP_ALLOW_SHELL=true` ile ilgili 32 test geçti. Tek gerçek sözleşme drift'i OpenAPI yeniden üretimiyle giderildi.
- Güncel full-suite: 2.873 test, OK (31 skipped), 231.789 saniye. Opt-in 27 gerçek tarayıcı testi ayrıca geçti. Sonradan test fixture/cleanup düzenlemesi yapılan scan/extraction modülleri ayrıca 12/12 geçti. `git diff --check` temiz. Testlerde mevcut ResourceWarning/DeprecationWarning mesajları var; sonuç exit 0.

Geçici kanıt logları: `/tmp/mac-mcp-215-full-tests-final.log`, `/tmp/mac-mcp-215-browser-tests-final.log`, `/tmp/mac-mcp-215-safari-check.log`. Safari harness: `/tmp/mac-mcp-215-safari-check.py`. Bunlar geçici dosyalardır; üretim anahtarı içermez.

Tekrar çalıştırma (worktree içinde, yalnız test ortamı):

```bash
MCP_ALLOW_SHELL=true MCP_API_KEY=local-test-only PYTHONDONTWRITEBYTECODE=1 \
  /Users/tarkanbulut/mac-mcp/.venv/bin/python -m unittest discover -s tests -q
MAC_MCP_BROWSER_INTEGRATION=1 MCP_API_KEY=local-test-only PYTHONDONTWRITEBYTECODE=1 \
  /Users/tarkanbulut/mac-mcp/.venv/bin/python -m unittest tests.test_browser_page_integration -q
git diff --check
```

## Kapsam ve merge

`complete` görülen/render edilmiş liste kapsamını anlatır; sitenin geç yüklenen verisinin, başka sayfaların veya total review rozetinin tamamına erişildiği garantisi değildir. Yeni içerik veya pagination ayrıca okunmalıdır. Statik veri çıkarmada üç çağrı hedefi, uzun sayfada kapsamı azaltmak için katı sınıra dönüştürülmedi. Güncel Fragrantica sayfasında eski selector önceki incelemede sıfır bulmuştu; gerçek 76 yorum sonucu bu çalışmada yeniden iddia edilmez.

Merge ana ajana bırakıldı. Kendi işi bitince önce `main` durumunu, branch diff'ini ve güncel testleri kontrol etmeli. Sonra bu branch'i merge edebilir. Runtime deployment ayrı işlem: bu worktree'yi çalışma zamanı üzerine kopyalamamalı; source/runtime reconciliation, selective promotion ve canlı sağlık/feature kontrolleri yapılmalı.
