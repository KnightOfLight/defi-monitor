# defi-monitor — Telegram-бот для моніторингу Saturn / Unitas / InfiniFi / Tori (Pendle trUSD)

Бот раз на 15–30 хвилин збирає ціни, TVL, стан ринку Pendle trUSD (Tori) та InfiniFi, порівнює з порогами з `config.yaml`
і надсилає алерти в Telegram. Щоп'ятниці о 10:00 за Києвом — дайджест із проханням записати поінти
(`/log …`), у суботу — нагадування, якщо не записали. Журнал — `journal.csv`. Окремо бот стежить за анонсами
TGE / airdrop усіх чотирьох проєктів і нагадує про підтверджену дату (`/tge`).

Хостинг — GitHub Actions, безкоштовно. Схема з двох репозиторіїв:

| Репозиторій | Видимість | Що в ньому |
|---|---|---|
| `defi-monitor` | **публічний** | тільки код і `config.yaml`. Публічний — щоб хвилини Actions були безлімітні |
| `defi-monitor-data` | **приватний** | `journal.csv` з поінтами, `history.csv`, `state.json`. Бот сам пише сюди після кожного запуску |

Токени, chat id, RPC-ключі — лише в GitHub Secrets публічного репо; у коді, файлах і логах їх немає.

---

## Що потрібно від вас

- Акаунти Telegram і GitHub.
- ~30 хвилин на кроки 1–8 (один раз).
- Щоп'ятниці — відповісти боту числами поінтів (Saturn, Unitas, InfiniFi, Tori Cores).

---

## Крок 1. Бот у @BotFather

1. У Telegram відкрийте **@BotFather** → `/newbot`.
2. Назва (наприклад `DeFi Monitor`) і username (має закінчуватись на `bot`).
3. BotFather надішле **токен** вигляду `1234567890:AAH…` — це `TELEGRAM_BOT_TOKEN`. Нікому не показуйте.
4. Відкрийте чат зі своїм ботом і натисніть **Start** — інакше бот не зможе вам писати.

## Крок 2. Ваш chat id

Напишіть боту будь-який текст, потім відкрийте в браузері
`https://api.telegram.org/bot<ТОКЕН>/getUpdates` (підставте токен). У відповіді знайдіть
`"chat":{"id":123456789,…}` — число і є `TELEGRAM_CHAT_ID`.
Якщо відповідь `{"ok":true,"result":[]}` — ви ще не написали боту (або написали іншому боту).

Простіше: `/start` боту **@userinfobot** — він одразу покаже ваш `Id`.

Для групи: додайте бота в групу, напишіть там `/start`; id групи від'ємний (`-1001234567890`).

## Крок 3. RPC (необов'язково, але краще зробити)

RPC — це вузол, через який бот читає блокчейн. Без налаштувань бот використовує публічні вузли
(`publicnode.com`, `llamarpc.com`); власний безкоштовний ключ надійніший.

alchemy.com → Sign up → **Create new app** → мережі **Ethereum** і **BNB Smart Chain** (Mainnet) → у створеному
app відкрийте **API key / Network** і скопіюйте HTTPS-адреси:
- `https://eth-mainnet.g.alchemy.com/v2/<ключ>` → секрет `ETH_RPC_URL`
- `https://bnb-mainnet.g.alchemy.com/v2/<ключ>` → секрет `BSC_RPC_URL`

Це теж секрети — не вставляйте їх нікуди, крім GitHub Secrets.

## Крок 4. Приватний репозиторій для даних

github.com → **New repository**:
- Repository name: `defi-monitor-data`
- **Private**
- поставте галочку **Add a README file** (зовсім порожній репозиторій воркфлоу взяти не зможе)
- **Create repository**. Більше в ньому нічого не робіть.

## Крок 5. Токен доступу до репозиторію даних

Фото профілю (праворуч угорі) → **Settings** → внизу зліва **Developer settings** →
**Personal access tokens → Fine-grained tokens → Generate new token**:

- Token name: `defi-monitor-data`
- Expiration: **1 year** (поставте собі нагадування оновити через рік)
- Repository access: **Only select repositories** → оберіть `defi-monitor-data`
- Permissions → **Repository permissions** → **Contents: Read and write** (решта не потрібна; Metadata додасться сам)
- **Generate token** → скопіюйте (показується один раз). Це `DATA_REPO_TOKEN`.

## Крок 6. Публічний репозиторій з кодом

1. **New repository** → назва `defi-monitor`, **Public**, без README → **Create repository**.
2. Розпакуйте архів `defi-monitor.zip` на комп'ютері (завантаження працює лише з комп'ютера, не з телефона).
3. На сторінці нового репозиторію натисніть посилання **uploading an existing file** (у блоці «Quick setup»).
   Перетягніть у вікно браузера **вміст** папки `defi-monitor`: папки `bot`, `tools`, `tests` і файли
   `config.yaml`, `requirements.txt`, `README.md`. → **Commit changes**.
4. Усе, що починається з крапки (`.github`, `.gitignore`), веб-завантажувач відкидає з написом
   «This file is hidden». Тому три воркфлоу додаємо вручну, по одному:
   **Add file → Create new file** → у полі імені введіть `.github/workflows/monitor.yml`
   (кожна коса риска створює папку) → відкрийте файл `defi-monitor\.github\workflows\monitor.yml`
   з архіву в Блокноті, Ctrl+A, Ctrl+C → вставте у велике поле на GitHub → **Commit changes**.
   Повторіть для `weekly.yml` і `selftest.yml`. `.gitignore` не потрібен.
5. Папки `data/` в публічному репозиторії **не має бути** — дані живуть у `defi-monitor-data`.

Гілка за замовчуванням має називатись `main` (GitHub робить це сам); розклад працює лише на ній.

## Крок 7. Секрети й дозволи (у публічному репо `defi-monitor`)

**Settings → Secrets and variables → Actions → New repository secret**, шість штук:

| Назва | Значення |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен із кроку 1 |
| `TELEGRAM_CHAT_ID` | число з кроку 2 |
| `ETH_RPC_URL` | адреса Ethereum із кроку 3 (можна пропустити) |
| `BSC_RPC_URL` | адреса BNB Chain із кроку 3 (можна пропустити) |
| `DATA_REPO` | `ваш_логін/defi-monitor-data` — саме так, через косу риску, без `https://` |
| `DATA_REPO_TOKEN` | токен із кроку 5 |

Потім **Settings → Actions → General**:
- Actions permissions: **Allow all actions and reusable workflows**;
- Workflow permissions: **Read and write permissions** → Save.

## Крок 8. Перший запуск

1. Вкладка **Actions** (верхнє меню репозиторію). Якщо GitHub просить підтвердити увімкнення воркфлоу — підтвердіть.
2. Зліва **Self-test (перевірка джерел)** → **Run workflow** → **Run workflow**. За хвилину в Telegram
   прийде тестове повідомлення і звіт: по кожному джерелу ✅ або ❌ з причиною. Усі рядки мають бути ✅.
   Якщо в Telegram нічого не прийшло — відкрийте лог запуску, рядок «Telegram sendMessage (chat id)» покаже, що не так.
3. **Monitor (every 30 min)** → **Run workflow**. Прийде «🤖 Бот запущено» зі статусом.
   Далі воркфлоу запускається сам.
4. Перевірка, що дані пишуться куди треба: у `defi-monitor-data` мають з'явитись `state.json` і `history.csv`,
   а в публічному `defi-monitor` **нових комітів бути не повинно**. Якщо крок «Safety check» у логах червоний —
   секрети `DATA_REPO` / `DATA_REPO_TOKEN` не задані або з помилкою; бот навмисно не запуститься, щоб не
   покласти журнал у публічний репозиторій.
5. Напишіть боту `/status` — відповідь прийде при наступному запуску (до 30 хв). Це фінальна перевірка, що
   команди читаються.
6. (За бажанням) **Weekly digest** → Run workflow → у полі `force` введіть `digest` — побачите дайджест і
   зможете одразу спробувати `/log`.

Коли все працює, інтервал можна зменшити: у `.github/workflows/monitor.yml` (олівець на GitHub) замініть
`"*/30 * * * *"` на `"*/15 * * * *"` → Commit. Для публічного репо хвилини безлімітні.

---

## Оновлення з v1.0 (додано Tori і стеження за TGE, прибрано пул Curve USDat)

Замініть у публічному репозиторії файли `bot/*.py`, `config.yaml`, `tests/test_offline.py`, `tools/find_pools.py`
і `README.md` (через **Add file → Upload files** — перетягніть папки `bot`, `tests`, `tools` і файли; GitHub замінить
однойменні). Воркфлоу і секрети не змінювались. Потім **Self-test** → Run workflow: мають бути ✅ нові рядки
«Curve frxUSD/trUSD», «Tori on-chain», «Pendle trUSD on-chain», «Pendle API», «DefiLlama tori» і рядки
«TGE · …» (Google News, Bing News, docs, Telegram; ⚠️ у них не критичне). При наступному запуску
монітора прийде «🔄 Бот оновлено до v1.1.0» зі статусом.

Журнал не втрачається: стовпці `curve_usdat_share` / `curve_pool_liquidity` у старих рядках лишаються, нові рядки
отримують `tori_points`, `tvl_tori`, `price_trusd`, `strusd_rate`, `pendle_pt_price`, `pendle_implied_apy`,
`pendle_liquidity`. Порівняння «за 24 год / 7 днів» для нових метрик з'являться через добу й тиждень.

## Команди

| Команда | Що робить |
|---|---|
| `/status` | поточні значення всіх метрик і рівень відносно порогів (✅ норма / ⚠️ увага / 🚨 тривога) |
| `/log saturn 123456 unitas 7890 infinifi 4567 tori 2500` | записати поінти в рядок журналу (п'ятничний, якщо він ще без поінтів; інакше — сьогоднішній). `tori` (або `cores`) — поінти Tori Cores. Можна вказати не всі; `123,456` і `123 456` розпізнаються; текст «saturn 123 tori 45» без `/log` теж приймається |
| `/note текст` | дописати примітку до останнього рядка |
| `/export` | надіслати `journal.csv` файлом + підсумок за тиждень і місяць (зручно вставити в Claude) |
| `/thresholds` | показати пороги |
| `/tge` | що відомо про TGE / airdrop кожного проєкту + останні знайдені новини |
| `/tge saturn 2026-11-20` | задати підтверджену дату TGE (також `20.11.2026`, `20.11`); нагадування за 7 і 1 день та в день TGE. `/tge saturn off` — прибрати |
| `/help` | довідка |

Бот відповідає лише на повідомлення з вашого `TELEGRAM_CHAT_ID`; чужі ігнорує. Відповідь приходить при
наступному запуску за розкладом, а не миттєво.

## Алерти

Два рівні — **⚠️ УВАГА** і **🚨 ТРИВОГА** — і окреме **✅ НОРМА** при поверненні. Один алерт на подію;
поки умова тримається — повтор раз на 6 годин; при ескалації (увага → тривога) — окреме повідомлення одразу.
Перші 7 днів (warm-up) бот лише збирає базу і не шле алерти по TVL та InfiniFi (налаштовується); ціни, Pendle,
strUSD і нагадування про погашення працюють одразу.
Якщо нічого не відбувається — бот мовчить до п'ятничного дайджесту.

| Сигнал | Увага | Тривога | Коли дізнаєтесь |
|---|---|---|---|
| Ціна USDat / USDu / iUSD / trUSD | < $0.995 на 2 перевірках поспіль | < $0.985 (USDu < $0.98) одразу | ~1 год / до 30 хв |
| TVL Saturn / Unitas / InfiniFi / Tori (DefiLlama) | −20% за 7 днів | −30% за 24 год | до 30 хв |
| TVL Saturn | ℹ️ при > $800 млн | — | до 30 хв |
| Pendle trUSD: ціна PT (у trUSD) | −0.5% за 24 год | −1.5% за 24 год | до 30 хв |
| Pendle trUSD: implied APY | > 30% на 2 перевірках (крім останніх 7 днів до погашення) | — | ~1 год |
| Pendle trUSD: ліквідність ринку | −25% за 24 год або −30% за 7 днів | — | до 30 хв |
| Pendle trUSD: погашення 26.11.2026 | ℹ️ за 14, 7 і 2 дні та в день погашення | — | — |
| Pendle: новий ринок trUSD (для перекладу) | ℹ️ одразу, як з'явиться | — | до 24 год |
| Tori: курс strUSD | — | будь-яке падіння (списаний збиток) | до 30 хв |
| InfiniFi: ліквідний резерв | < 5% TVL три дні поспіль | — | через 3 дні |
| InfiniFi: черга redemption | — | не порожня понад 48 год | через 48 год |
| InfiniFi: exchange rate liUSD-13w | — | будь-яке падіння | до 30 хв |
| Джерело даних не відповідає | 3 запуски поспіль | — | ~1,5 год |
| 📣 Анонс TGE / airdrop / лістингу | окреме повідомлення з посиланням і датами з тексту | — | перевірка раз на 2 год |
| ⏰ Підтверджена дата TGE (`/tge`) | за 7 і 1 день, 🚀 у день TGE | — | — |
| Джерело TGE-новин не відповідає | 6 перевірок поспіль (~12 год) | — | — |

GitHub іноді запускає розклад із затримкою 5–20 хв — це нормально.

## Як міняти пороги

Усі пороги — у `config.yaml` публічного репо: відкрийте файл → олівець → змініть число → **Commit changes**.
Наступний запуск уже працюватиме з новими значеннями.

Найчастіші правки:
- `alerts.warmup_suppress: [tvl, infinifi]` — приберіть `infinifi`, якщо хочете алерти InfiniFi
  (падіння rate, черга) одразу, без warm-up.
- `alerts.pendle.*` — пороги для ринку Pendle trUSD (див. «Tori на Pendle» нижче).
- Після погашення 26.11.2026, якщо перекладете позицію в новий ринок: замініть `sources.ethereum.pendle_trusd_market`
  і `pendle_trusd_pt` на адреси нового ринку (бот сам надішле їх, щойно ринок з'явиться).
- `alerts.repeat_hours: 6` — частота повторів.
- `sources.defillama.slugs.unitas: unitas-usdu` — дочірній протокол «Unitas USDu» (саме USDu, BSC + Solana).
  Батьківський `unitas` рахує разом із sUSDu та іншими продуктами.
- Адреси пулів — у `sources.ethereum` / `sources.bsc`. Знайти найглибший пул для токена:
  `python tools/find_pools.py bsc 0xea953ea6634d55dac6697c436b1e81a679db5882`.

## Tori на Pendle — що і навіщо бот дивиться

Позиція — у ринку Pendle **trUSD 26NOV2026** (`0xfcf0…c947`). trUSD сам дохід не нараховує (на Pendle
`underlyingApy = 0`), тож implied APY ринку — це по суті ринкова ціна поінтів Tori Cores. Що може піти не так і як бот це ловить:

- **trUSD втрачає вартість.** trUSD — синтетичний долар на дельта-нейтральних стратегіях (як USDe), погашення напряму
  лише для whitelisted, усі інші виходять через DEX. Ціна trUSD — з пулу Curve frxUSD/trUSD (продаж 1 000 trUSD),
  резерв — DefiLlama coins.
- **Збиток у стратегіях Tori.** Просадки стратегій спершу покриває резервний фонд Tori (розмір — в атестаціях
  tori.accountable.capital). Якщо збиток списують через `StakedTrUSD.reportLoss`, спалюється trUSD зі сховища strUSD
  (не більше 5% сховища на добу) — тобто strUSD стоїть між збитком і звичайним trUSD. За кодом контракту курс strUSD
  падає лише так, тому будь-яке його падіння — найчистіший on-chain сигнал проблем, навіть якщо trUSD ще тримає $1.
  `/status` показує курс strUSD і яка частка всього trUSD у ньому лежить.
- **Ринок починає закладати ризик.** У нормі PT щодня трохи дорожчає до 1 trUSD. Якщо PT дешевшає за добу — хтось
  продає PT або масово купує YT; бот повідомить і підкаже, як відрізнити одне від іншого. Implied APY перед самим
  погашенням дуже шумний (малий рух ціни = великий стрибок APY), тому в останні 7 днів алерт по APY вимкнений.
- **LP виходять.** Ліквідність ринку ~$1,1 млн; якщо вона тане — вихід із PT/LP стане дорожчим.
- **Погашення.** Після 26.11.2026 позиція нічого не заробляє. Бот нагадає за 14, 7 і 2 дні, у день погашення
  підкаже, що робити з PT/LP/YT, і раз на добу перевіряє через Pendle API, чи не з'явився новий ринок trUSD.

Моніторинг пулу Curve USDC/USDat (частка USDat і ліквідність) з v1.1 прибраний; ціна USDat лишилась.

## TGE / airdrop — як бот дізнається

Що відомо станом на 20.10.2026 (це ж бот показує в `/tge`; тексти — у `config.yaml`, блок `tge.projects`):

| Проєкт | Токен | TGE |
|---|---|---|
| Saturn | STRN | Q4 2026, дати немає. Snapshot сезону 2 — перед TGE, якщо TGE до 08.12.2026 |
| Unitas | $UP | **уже відбувся** 13.03.2026 (Binance Wallet); бот стежить за новими сезонами/розподілами |
| infiniFi | INFI | Q4 2026, дати немає; токеноміка опублікована 14.10.2026 |
| Tori | — | токен не анонсовано; поінти Cores, сезон 1 |

Усі чотири анонсують в X (Twitter), а X без платного API не читається. Тому бот бере те, що звідти за кілька годин
потрапляє у відкритий веб:

- **Google News і Bing News** — пошук по кожному проєкту з ключовими словами (TGE, airdrop, tokenomics, listing, claim,
  snapshot, season…), раз на 2 години. Сюди потрапляють передруки анонсів і новини бірж про лістинг. Bing бачить
  більше дрібних крипто-сайтів (наприклад, KuCoin flash з токеномікою infiniFi 13.10, якої в Google News немає).
- **Публічні Telegram-канали** — щозапуску (раз на 15 хв): новини Wu Blockchain (англ.), Foresight News і BlockBeats
  (кит., найшвидше пишуть про TGE), а також Binance і Bybit Announcements (лістинги, Binance Alpha, HODLer, Booster —
  через Binance Wallet пройшов TGE Unitas). Свої канали — у `tge.telegram_channels`. Тут потрібна точна назва
  (`Saturn Foundation`, `STRN`, `USDat`…), бо просто «Saturn» — це й ракета, і планета; для китайських текстів є
  ключові слова 空投, 代币经济学, 快照 тощо.
- **Документація проєктів** (`sitemap.xml`) — нова сторінка зі словами token / tokenomics / tge / airdrop / claim /
  season / тікер у шляху часто з'являється до офіційного анонсу.

Перший прохід кожного джерела тихий (бот запам'ятовує, що вже є), далі приходять лише нові згадки. Той самий сюжет
з різних сайтів — одне повідомлення. Дати з тексту («November 18», «Q4 2026») бот виносить окремо. Коли дату
підтверджено — `/tge infinifi 2026-11-18`, і бот нагадає за 7 і 1 день, а в день TGE попередить про фішингові claim-сайти.

Обмеження: затримка відносно X — від 15 хвилин (якщо підхопили Telegram-канали) до кількох годин; анонс лише в Discord бот не побачить. Для TGE цього
зазвичай досить: дату оголошують за дні–тижні.

## Звідки беруться дані (перевірено 09–12.10.2026; Tori — 20.10.2026)

| Метрика | Джерело | Примітка |
|---|---|---|
| TVL ×4 | DefiLlama `api.llama.fi/tvl/<slug>` (резерв `/protocol/<slug>`) | slug-и `saturn`, `unitas-usdu`, `infinifi`, `tori-finance` |
| Ціна USDat | Curve-пул `0xf4d0…96d7` через `eth_call` (`get_dy`) | продаж 1 000 USDat → ціна в USDC |
| Ціна USDu | PancakeSwap V3 USDu/USDT 0.01% `0x8aab…2120`, `slot0()` через `eth_call` | резерв — GeckoTerminal; ліквідність ~$0.7 млн |
| Ціна iUSD | Curve iUSD/USDC `0x2e2d…986e`, `get_dy` на 100 iUSD | **пул ~$8–10 тис.** — ціна індикативна; дивіться також курс погашення у `/status` |
| Ціна trUSD | Curve frxUSD/trUSD `0xd1d9…6ff`, `get_dy` на 1 000 trUSD | ціна у frxUSD; резерв — `coins.llama.fi`. Найбільший обіг trUSD — на Fluid (USDC), але Fluid DEX бот не читає |
| PT-trUSD, implied APY, ліквідність ринку, дні до погашення | Pendle-ринок `0xfcf0…c947`: `expiry()`, `readTokens()`, `_storage()` (totalPt, totalSy, lastLnImpliedRate), SY `exchangeRate()` | APY = e^lnRate − 1; PT = e^(−lnRate·t/рік); резерв — `api-v2.pendle.finance` |
| Курс strUSD, буфер strUSD, trUSD в обігу | strUSD `0x2808…5815` (ERC-4626): `convertToAssets(1e18)`, `totalAssets()`; trUSD `0xd058…1697`: `totalSupply()` | буфер = trUSD у strUSD / весь trUSD |
| Нові ринки trUSD | `api-v2.pendle.finance/core/v1/1/markets/active`, фільтр за underlying trUSD | раз на добу |
| Rate liUSD-13w, резерв, черга, курс погашення, unaccrued yield | On-chain через Gateway `0x3f04…25b5`: `getAddress("lockingController").exchangeRate(13)`, `redeemController.queueLength()` / `totalEnqueuedRedemptions()` / `receiptToAsset()`, `accounting.totalAssetsValue()` / `totalAssetsValueOf(LIQUID)` | публічного API в InfiniFi немає — docs відсилають до приватного репо; on-chain дає ті самі числа |

Self-test перевіряє всі джерела і додатково звіряє, що `shareToken(13)` у LockingController дорівнює адресі
liUSD-13w з docs, PT з `readTokens()` ринку Pendle — адресі `pendle_trusd_pt`, а `strUSD.asset()` — адресі trUSD.

## Що публічне, а що ні

У публічному `defi-monitor` видно: код, `config.yaml` (пороги й адреси контрактів — вони й так публічні),
логи запусків (кількість метрик/алертів, назви джерел, публічні ринкові числа у self-test). Ключі, токени,
адреси RPC з ключами і username бота з логів вичищені.

Ніхто не бачить: `journal.csv` з поінтами, `history.csv`, `state.json` (усе в приватному `defi-monitor-data`),
секрети. Єдине, що лишається видимим і стосується вас: той, хто знайде репо, зрозуміє, що власник акаунта
цікавиться цими протоколами. Якщо й це зайве — створіть публічне репо з окремого анонімного акаунта.

## Якщо щось не працює

| Симптом | Що робити |
|---|---|
| Крок «Safety check» червоний | не задані або з помилкою `DATA_REPO` / `DATA_REPO_TOKEN`. `DATA_REPO` — у форматі `логін/defi-monitor-data` |
| Крок «Checkout private data repo» червоний | токен без права Contents: Read and write, або вибраний не той репозиторій, або токен прострочений (через рік) — згенеруйте новий і оновіть секрет |
| Крок «Commit state» червоний, push rejected | рідкісний збіг двох запусків — наступний запуск усе виправить |
| Self-test: ❌ DefiLlama … slug не знайдено | відкрийте `https://defillama.com/protocol/<slug>`; виправте slug у `config.yaml` |
| Self-test: ❌ Ethereum RPC / BSC RPC | вузол тимчасово лежить або невірний URL у секреті; бот сам перемикається на резервні публічні вузли |
| ❌ Curve … «Токен не знайдено в пулі» | адреса пулу або токена змінилась — знайдіть пул через `tools/find_pools.py` |
| ❌ InfiniFi «адресу не знайдено в Gateway» | адреса Gateway змінилась — перевірте https://docs.infinifi.xyz/dev-docs/contracts.md |
| ❌ Pendle trUSD on-chain / «PT … НІ» | адреса ринку в `pendle_trusd_market` не та; візьміть її з app.pendle.finance (URL сторінки ринку) або з алерту про новий ринок |
| ❌ Curve frxUSD/trUSD | пул спорожнів або змінився — бот бере ціну з DefiLlama coins; новий пул: `python tools/find_pools.py eth 0xd0580192E98eA6CEB9c7b6191Ed2E27560911697` |
| ❌ Pendle API | не критично: це лише резерв і пошук нових ринків, основні дані — on-chain |
| ⚠️ TGE · Google News / Bing News / Telegram / docs (self-test не валять) | тимчасово — само відновиться; якщо постійно: для docs перевірте адресу `docs_sitemap`, для Telegram — що канал публічний (`t.me/s/<назва>` відкривається в браузері) |
| Забагато TGE-повідомлень не про ваш проєкт | приберіть надто загальне слово з `aliases` або `keywords` (наприклад `season`) у `config.yaml` |
| Self-test: «Bing … 0 статей» | для Bing у `bing_queries` — лише прості запити без `OR` і дужок; якщо 0 і з простими — Bing не віддає новини серверам GitHub, приберіть `bing` з `news_engines` |
| Хибна «нова сторінка документації» | додайте розділ у `docs_ignore` проєкту (як `/dev-docs/` для infiniFi) |
| Бот не відповідає на команди | натисніть Start у чаті з ботом; перевірте `TELEGRAM_CHAT_ID`; зачекайте наступного запуску |
| Telegram-помилка 409 Conflict у логах | два боти з одним токеном (старий репозиторій не видалений або працює `loop` на сервері) — залиште один |
| Алерт «Джерело даних не відповідає» | подивіться текст помилки; якщо тимчасово — само відновиться і прийде ✅ |
| Воркфлоу не з'являються у вкладці Actions | файли не в `.github/workflows/` або помилка відступів у YAML — порівняйте з архівом |

## Альтернативи

**Один приватний репозиторій без `defi-monitor-data`.** Ті самі воркфлоу працюють і так: якщо секрету
`DATA_REPO_TOKEN` немає, бот комітить `data/` прямо в репозиторій. Але тоді репо має бути **приватним**
(у публічному крок «Safety check» зупинить запуск), а ліміт — 2 000 хв/міс: інтервал 30 хв ≈ 1 500 хв,
15 хв не влазить.

**Миттєві відповіді на команди.** GitHub Actions відповідає з затримкою до 30 хв. Той самий код працює як
постійний процес на будь-якому Linux-сервері (безкоштовно — Oracle Cloud Always Free):

```bash
git clone <публічний репозиторій> && cd defi-monitor
python3 -m pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… ETH_RPC_URL=… BSC_RPC_URL=…
python3 -m bot.run loop --interval 15
```

Для автозапуску — systemd-сервіс із `Restart=always`. Одночасно має працювати лише один режим (або Actions,
або `loop`): два процеси з одним токеном конфліктують на `getUpdates`.

## Структура репозиторію

```
defi-monitor/
├── .github/workflows/
│   ├── monitor.yml      # кожні 30 хв: safety check → приватне репо даних → метрики → алерти → команди → commit
│   ├── weekly.yml       # пт/сб 07:00 і 08:00 UTC (= 10:00 Київ влітку/взимку): дайджест / нагадування
│   └── selftest.yml     # ручна перевірка всіх джерел зі звітом у Telegram
├── bot/
│   ├── run.py           # точка входу: monitor | weekly | selftest | status | loop
│   ├── config.py        # config.yaml + секрети з env
│   ├── evm.py           # eth_call через JSON-RPC без web3; селектори обчислені keccak256; ключі маскуються
│   ├── sources.py       # DefiLlama, Curve, PancakeSwap V3, InfiniFi on-chain, Pendle, Tori, GeckoTerminal
│   ├── metrics.py       # збір усіх метрик, стійкий до падіння окремого джерела
│   ├── alerts.py        # пороги, ескалація, антиспам, warm-up, «норма», погашення Pendle
│   ├── commands.py      # /status /log /note /export /thresholds /help
│   ├── weekly.py        # п'ятничний дайджест і суботнє нагадування
│   ├── tge.py           # анонси TGE/airdrop: Google News, Telegram-канали, sitemap документації; /tge
│   ├── storage.py       # state.json, history.csv, journal.csv
│   ├── telegram.py      # Telegram Bot API напряму
│   └── fmt.py           # форматування чисел
├── tools/find_pools.py  # найглибші пули токена (GeckoTerminal)
├── tests/test_offline.py# офлайн-тести: python -m unittest tests.test_offline
├── config.yaml          # пороги й адреси
└── requirements.txt     # requests, PyYAML, tzdata
```

Файли даних (`journal.csv`, `history.csv`, `state.json`) створюються автоматично і живуть у `defi-monitor-data`.
Залежності: Python 3.11+, `requests`, `PyYAML`, `tzdata`. Жодних web3/eth_abi.
