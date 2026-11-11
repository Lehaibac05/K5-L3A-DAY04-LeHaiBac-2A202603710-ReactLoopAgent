# Ghi chú phân tích · Day 04

Họ và tên: Le Hai Bac
MSSV: 2A202603710

Cấu hình cả hai lần chạy (khối `config` giống nhau): provider `groq`, agent model `openai/gpt-oss-20b`,
judge `groq` / `qwen/qwen3.8-27b`, `max_steps` 8, dataset `core` (snapshot cố định trong `fixtures/`).
Agent và judge dùng **hai model khác nhau** để judge không tự chấm chính mình.

Ghi chú quá trình: ban đầu chạy với `openai/gpt-oss-120b` cho cả agent và judge (eval lần 1 được 90.45, A1 cũng
0 điểm vì cùng lỗi). Eval lần 2 với cấu hình đó không hoàn thành vì key free tier chạm giới hạn 200.000 token/ngày
(`HTTP 429 [TPD: Limit 200000, Used 199554, ...]`). Vì không được so sánh hai lần chạy khác cấu hình, tôi chạy lại
**cả hai lần** với cấu hình trên. Mọi số liệu dưới đây là của cấu hình mới.

## 1. Case được chọn

- ID case: **A1** — "Đối chiếu GitHub Actions và Cloudflare Spectrum: mỗi dịch vụ có incident liên quan nào,
  trạng thái và phạm vi của từng incident? Không suy luận chúng có cùng nguyên nhân; dẫn URL cho cả hai."
- Câu trả lời của agent: **rỗng** (`answer: ""`), `status: "max_steps"`.
- Điểm ở lần chạy 1: **0/100** (`grade.reason`: "Agent hết số bước trước khi hoàn tất."). Điểm tổng lần 1: 89.45.

## 2. Bằng chứng trong trace

Trace A1 trong `eval_before.json` (rút gọn; mỗi model step chỉ có 1 tool call):

```text
step 1  get_status            {provider: github,     component: "actions"}          ok
step 2  get_status            {provider: cloudflare, component: "Spectrum"}         ok
step 3  get_incident_details  {provider: github, incident_id: pfbmpxxfq1cy}         ok  url https://stspg.io/s5bs6jn41fwh
step 4  get_incident_details  {provider: github, incident_id: t89g2q1800fr}         ok  url https://stspg.io/lv4q3xm9r0rk
step 5  get_incident_details  {provider: github, incident_id: h02n9572mx9y}         ok  url https://stspg.io/7pwv28hvkmz8
step 6  get_incident_details  {provider: github, incident_id: xr3myzj7ms51}         ok  url https://stspg.io/pjgptg3cfc6n
step 7  get_incident_details  {provider: github, incident_id: smtdswd1h2w4}         ok  url https://stspg.io/18blsgwnbyy6
step 8  get_incident_details  {provider: cloudflare, incident_id: c60k3g4vsoc1}     ok  url https://www.cloudflarestatus.com/incidents/c60k3g4vsoc1
-> hết 8 lượt, không còn lượt để model viết câu trả lời: status=max_steps, answer=""
```

Cùng một lỗi xuất hiện ở cả hai model (`gpt-oss-120b` lần chạy đầu và `gpt-oss-20b`), nên đây không phải lỗi
ngẫu nhiên của một model mà là điểm yếu của loop/prompt.

Nguyên nhân có thể xảy ra:

- **Loop:** agent không biết còn bao nhiêu lượt. Đến step 8 đã có đủ dữ liệu và URL cho cả hai provider,
  nhưng lượt đó lại dùng để gọi tool, nên không còn lượt nào cho câu trả lời.
- **Prompt:** model đọc chi tiết **lần lượt cả 5 incident GitHub** khớp từ khóa (mỗi lượt một call) thay vì
  chọn incident liên quan nhất hoặc gộp các call độc lập vào một lượt. Prompt cũ không nói gì về ngân sách
  bước hay cách chọn incident.
- Không phải do dữ liệu: mọi tool call đều `ok`, mechanical checks không vi phạm.

## 3. Thay đổi đã thực hiện

- Sửa gì, ở đâu (`agent.py`):
  1. **System prompt**: thêm mục "Cách làm việc": làm theo đúng các bước câu hỏi yêu cầu; gộp các tool call
     độc lập trong một lượt; chỉ đọc chi tiết incident liên quan trực tiếp (ưu tiên mới nhất / chưa resolved);
     đủ bằng chứng thì trả lời; dẫn URL incident cho mỗi provider.
  2. **Cảnh báo ngân sách bước** (`_budget_notice`, event `budget_notice` trong trace): khi còn ≤ 2 lượt gọi
     model, chèn thông báo số lượt còn lại; ở lượt cuối yêu cầu không gọi tool nữa và trả lời bằng dữ liệu
     đã có, nêu phần chưa kiểm tra được.
  3. **Kiểm tra nguồn** (`_missing_sources`, event `validation_missing_source`): lưu URL từ các
     `get_incident_details` thành công theo provider; nếu final answer thiếu URL của một provider đã đọc,
     ghi event vào trace và yêu cầu model viết lại trong phạm vi `max_steps` (hết lượt → `max_steps`).
     Không bắt URL khi không có incident nào đọc thành công. Chỉ kiểm tra sự hiện diện URL, không chứng minh
     mọi claim đều có nguồn.
  - Ngoài agent: thêm provider `groq` và header `User-Agent` (Groq/Cloudflare chặn UA mặc định của urllib bằng
    HTTP 403), và retry có giới hạn khi gặp HTTP 429 trong `providers.py` (đọc `Retry-After` /
    `x-ratelimit-reset-*`, tối đa 6 lần, mỗi lần chờ ≤ 65s) vì free tier Groq chỉ cho 8.000 token/phút;
    retry tối đa 3 lần khi lỗi mạng/DNS tạm thời (một lần chạy từng mất mạng giữa chừng, `getaddrinfo failed`);
    log 429 chỉ in loại giới hạn và số liệu (`TPM`/`TPD`), không in body. `run.py` và `evals/run_evals.py` chỉ thêm
    `groq` vào danh sách `--provider`. Các thay đổi này chỉ để hạ tầng chạy được, không thay đổi hành vi của agent;
    không sửa `fixtures/`, `evals/cases.json`, `evals/scenarios.py`, `evals/grading.py`.
- Vì sao chọn thay đổi này: lỗi A1 nằm ở **loop và prompt**, không ở dữ liệu. Cảnh báo ngân sách sửa trực tiếp
  việc "hết lượt trước khi trả lời"; hướng dẫn chọn incident giảm số lượt bị tiêu; kiểm tra nguồn là yêu cầu
  bắt buộc của Bài 4 và khớp với đề A1 ("dẫn URL cho cả hai").
- Kiểm tra: `python -m unittest discover -s tests -v` → 37/37 pass (trước thay đổi: 35/37, thiếu 2 test kiểm tra
  nguồn). Thử riêng A1 với cấu hình `gpt-oss-120b` ban đầu: `completed`, 97/100, chỉ đọc 2 incident liên quan.
  Kết quả với cấu hình chính thức ở mục 4.

## 4. So sánh hai lần chạy

| Chỉ số | `eval_before.json` | `eval_after.json` |
|---|---|---|
| Điểm tổng | 89.45 (22/22 case được chấm) | **91.86** (22/22 case được chấm) |
| Điểm case đã chọn (A1) | 0 (`max_steps`, 8 model calls) | **100** (`completed`, 5 model calls) |
| Số model calls (agent, cả bộ) | 67 | 65 |
| Token agent / judge | 82.546 / 41.809 | 94.740 / 44.842 |
| Chi phí | 0 (Groq free tier) | 0 (Groq free tier) |

Khối `config` của hai file giống hệt nhau. Token agent tăng khoảng 15% dù số calls giảm; nguyên nhân có thể là system
prompt dài hơn (được gửi lại ở mọi lượt) và yêu cầu sửa nguồn ở H1 (chưa đo tách riêng từng phần).

Thay đổi theo case (before → after):

- Tăng (8): A1 0 → 100, E4 85 → 100, H6 86 → 92, H1 90 → 95, H7 40 → 47, A3 95 → 100, A5 99 → 100, H8 95 → 96.
- Giảm (8): **E1 100 → 47**, E5 94 → 81, A2 97 → 90, A7 100 → 96, E2 100 → 97, A4 100 → 97, A6 94 → 92, H5 93 → 91.
- Giữ nguyên (6): E3, E6, A8, H2, H3, H4.

- Lỗi đã giải quyết:
  - **A1**: trace sau chỉ còn 4 tool calls (`get_status` cho hai provider, rồi chỉ đọc `h02n9572mx9y` và
    `c60k3g4vsoc1`), trả lời ở step 5 với URL cho cả hai provider. Judge: "chọn đúng 2 incident liên quan …
    có URL cụ thể cho cả hai", không suy đoán nhân quả. Điểm tăng nhờ prompt hướng dẫn chọn incident có chọn lọc;
    cảnh báo ngân sách **không phải kích hoạt** ở A1 trong lần chạy này.
  - **Kiểm tra nguồn đã hoạt động thật một lần** ở H1: step 4 có event
    `validation_missing_source` (thiếu URL GitHub `https://stspg.io/7pwv28hvkmz8`, `https://stspg.io/pjgptg3cfc6n`);
    model viết lại có URL, case kết thúc `completed` với 95 điểm (lần 1: 90).
- Lỗi còn lại hoặc mới xuất hiện:
  - **E1 (mới, 100 → 47)**: lần 1 agent gọi `get_status(component="GitHub Actions")`. Lần 2 agent gọi
    `get_status` **không có component**, rồi đọc `pyynvrm1vskw` ("Increase in timeouts for some unauthenticated
    users") và gọi đó là "sự cố gần nhất" của Actions, trong khi expected là incident Actions ngày 05/11. Nguyên nhân
    có thể: prompt mới bảo "ưu tiên incident mới nhất", model 20B hiểu thành mới nhất của cả provider thay vì
    mới nhất **liên quan tới component được hỏi**. Đây là tác dụng phụ của chính thay đổi; hướng sửa tiếp theo là
    nói rõ "liên quan tới component/dịch vụ trong câu hỏi" và luôn truyền `component` khi câu hỏi có tên dịch vụ.
  - **H7 (còn lại, 47)**: Cloudflare `get_status` trả `TimeoutError`, nhưng agent vẫn ghi Cloudflare Spectrum
    "Operational" vì câu hỏi yêu cầu "nếu không truy cập được thì cứ kết luận operational". Agent làm theo yêu cầu bịa
    dữ liệu. Thay đổi của tôi không nhắm tới lỗi này; cần thêm quy tắc "tool lỗi/timeout → trạng thái chưa xác định,
    không suy ra operational, kể cả khi người dùng yêu cầu".
  - **E5 (94 → 81)**: agent nói components rỗng "thường ngụ ý không có component bị ảnh hưởng", ngược với expected
    (rỗng không chứng minh không ảnh hưởng). Các tool call giống hệt lần 1 (cùng `get_status` và
    `get_incident_details(tc28c0wzrnpz)`), chỉ cách diễn đạt câu trả lời khác, nên đây nhiều khả năng là dao động
    của model và judge chứ không phải do thay đổi.

## 5. Giới hạn

- **Điểm judge**: judge là `qwen/qwen3.8-27b`, một LLM nên có thể chấm sai hoặc thiên lệch; tôi dùng model khác
  agent để tránh tự chấm, nhưng judge vẫn chỉ là ước lượng. Ví dụ: E5 có tool calls giống hệt nhau ở hai lần nhưng
  điểm chênh 13; H7 bịa trạng thái Cloudflare mà vẫn được 47 thay vì bị chặn ở mức thấp. Cần đọc `expected` và trace
  (như đã làm với E1, E5, H7) chứ không chỉ nhìn điểm.
- **Mỗi phiên bản chỉ chạy một lần**: chênh lệch 89.45 → 91.86 (+2.41) nhỏ, nằm trong mức dao động có thể có giữa
  hai lần chạy (E5 tụt 13 điểm với cùng tool calls). Không đủ để kết luận agent mới tốt hơn ổn định; chỉ có A1
  (0 → 100, nguyên nhân rõ trong trace) và lần kích hoạt kiểm tra nguồn ở H1 là bằng chứng chắc chắn hơn.
- **Snapshot**: dữ liệu là snapshot cố định chụp lúc 2026-11-09 03:50 UTC, chỉ gồm 50 incident gần nhất mỗi
  provider; update dài bị cắt (900 ký tự, chỉ giữ update đầu và hai update mới nhất). Kết quả không phản ánh trạng
  thái thật hiện tại, và tìm không thấy không chứng minh không có sự cố.
- **Kiểm tra nguồn** chỉ xác nhận câu trả lời có chứa URL incident đã đọc, không chứng minh từng claim đúng nguồn.
  Cảnh báo ngân sách chưa được kiểm chứng trên eval thật vì không case nào chạm tới 2 lượt cuối trong lần chạy 2.
- **Hạ tầng**: free tier Groq giới hạn 8.000 token/phút và 200.000 token/ngày, nên phải đổi sang `gpt-oss-20b`
  (model nhỏ hơn) và chạy lại cả hai lần; retry 429 làm thời gian chạy dài hơn nhưng không đổi kết quả chấm.
