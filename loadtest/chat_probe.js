// Phan dung chung cho ca hai kich ban tai (30 user va 50 user).
//
// Ly do ton tai: tieu chi "HTTP 200 + [DONE]" khong phat hien duoc cau tra loi
// rong. Ung dung van dong stream binh thuong va van ket bang cau "Da hoan tat
// truy van..." ngay ca khi run_sql nem loi, nen mot dot do co the bao 100%
// thanh cong trong khi tang CSDL hong hoan toan. Vi vay o day bat buoc phai co
// su kien dataframe thi moi tinh la thanh cong.

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

export const QUESTIONS = [
  'Co bao nhieu nguoi dung trong he thong?',
  'Co bao nhieu don vi trong bang units?',
  'Co bao nhieu spdv_tickets?',
  'Dem so nguoi dung theo trang thai',
  'Liet ke 5 don vi dau tien theo ten',
  'Co bao nhieu su kien trong spdv_ticket_events?',
];

export const aiResponseTime = new Trend('ai_response_time', true);
// Thanh cong chat: co du lieu that tra ve.
export const aiSuccessRate = new Rate('ai_success_rate');
// Tieu chi long cua cac dot do truoc (chi 200 + [DONE]), giu lai de so sanh.
export const aiStreamRate = new Rate('ai_stream_rate');
export const aiDataRate = new Rate('ai_data_rate');
export const aiErrors = new Counter('ai_errors');
export const httpLock409 = new Counter('lock_conflicts_409');
export const aiEmptyAnswers = new Counter('ai_empty_answers');

// Tach do tre theo tung cau hoi: cac cau khop luat tat dinh trong main.py khong
// he goi model, gop chung vao mot phan bo se giau mat chi phi that cua LLM.
const perQuestion = QUESTIONS.map((_, i) => new Trend(`ai_time_q${i + 1}`, true));
// Trend khong xuat ra so lan do, nen dem rieng; ty le rong theo cau hoi cho biet
// duong nao that su hay tra loi suong.
const perQuestionCalls = QUESTIONS.map((_, i) => new Counter(`ai_calls_q${i + 1}`));
const perQuestionData = QUESTIONS.map((_, i) => new Rate(`ai_data_q${i + 1}`));

export function askRandomQuestion(endpoint, reqTimeout, prefix) {
  const index = Math.floor(Math.random() * QUESTIONS.length);
  const question = QUESTIONS[index];
  const uid = `${prefix}-${__VU}-${__ITER}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

  const res = http.post(
    endpoint,
    JSON.stringify({ message: question, conversation_id: uid, request_id: uid }),
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: reqTimeout,
      tags: { name: 'chat_sse', question: `q${index + 1}` },
    }
  );

  const body = String(res.body);
  const streamed = res.status === 200 && body.includes('data: [DONE]');
  const hasData = body.includes('"type":"dataframe"');
  const ok = streamed && hasData;

  aiResponseTime.add(res.timings.duration);
  perQuestion[index].add(res.timings.duration);
  perQuestionCalls[index].add(1);
  perQuestionData[index].add(hasData);
  aiStreamRate.add(streamed);
  aiDataRate.add(hasData);
  aiSuccessRate.add(ok);
  if (res.status === 409) httpLock409.add(1);
  if (!ok) aiErrors.add(1);
  // Stream tron ven nhung khong co du lieu: dung loi im lang can theo doi rieng.
  if (streamed && !hasData) aiEmptyAnswers.add(1);

  check(res, {
    'HTTP 200': (r) => r.status === 200,
    'stream hoan tat ([DONE])': () => streamed,
    'co du lieu tra ve (dataframe)': () => hasData,
  });

  return res;
}

export function textSummary(data, title) {
  const m = data.metrics;
  const g = (name, sub) => {
    if (!m[name] || !m[name].values) return 'n/a';
    const v = m[name].values[sub];
    return v === undefined ? 'n/a' : Number(v).toFixed(2);
  };
  const pct = (name) =>
    m[name] && m[name].values ? (Number(m[name].values.rate) * 100).toFixed(1) + '%' : 'n/a';

  const lines = [];
  lines.push(`============ ${title} ============`);
  lines.push(`Tong so request:        ${g('http_reqs', 'count')}`);
  lines.push(`Request/giay (RPS):     ${g('http_reqs', 'rate')}`);
  lines.push(`Thanh cong (co du lieu):${pct('ai_success_rate')}`);
  lines.push(`  stream tron ven:      ${pct('ai_stream_rate')}`);
  lines.push(`  tra ve dataframe:     ${pct('ai_data_rate')}`);
  lines.push(`Tra loi rong:           ${g('ai_empty_answers', 'count')}`);
  lines.push(`So loi:                 ${g('ai_errors', 'count')}`);
  lines.push(`Xung dot khoa (409):    ${g('lock_conflicts_409', 'count')}`);
  lines.push('-------- Thoi gian phan hoi AI (ms) --------');
  lines.push(`  trung binh (avg):     ${g('ai_response_time', 'avg')}`);
  lines.push(`  trung vi  (median):   ${g('ai_response_time', 'med')}`);
  lines.push(`  nhanh nhat (min):     ${g('ai_response_time', 'min')}`);
  lines.push(`  cham nhat (max):      ${g('ai_response_time', 'max')}`);
  lines.push(`  p(90):                ${g('ai_response_time', 'p(90)')}`);
  lines.push(`  p(95):                ${g('ai_response_time', 'p(95)')}`);
  lines.push('-------- Theo tung cau hoi (so lan | trung vi | p95 (ms) | co du lieu) --------');
  QUESTIONS.forEach((q, i) => {
    const t = `ai_time_q${i + 1}`;
    lines.push(
      `  q${i + 1} ${g(`ai_calls_q${i + 1}`, 'count').padStart(6)} | ${g(t, 'med').padStart(10)} | ` +
        `${g(t, 'p(95)').padStart(10)} | ${pct(`ai_data_q${i + 1}`).padStart(6)}  ${q}`
    );
  });
  lines.push('='.repeat(title.length + 26));
  return lines.join('\n') + '\n';
}
