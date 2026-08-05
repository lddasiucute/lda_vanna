import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

// ---- Cau hinh (co the override bang bien moi truong -e) ----
const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const ENDPOINT = `${BASE_URL}/api/vanna/v2/chat_sse`;
const REQ_TIMEOUT = __ENV.REQ_TIMEOUT || '180s';

// ---- Metric tuy chinh ----
const aiResponseTime = new Trend('ai_response_time', true); // thoi gian AI tra loi tron ven
const aiSuccessRate = new Rate('ai_success_rate');          // % request thanh cong
const aiErrors = new Counter('ai_errors');                  // so loi
const httpLock409 = new Counter('lock_conflicts_409');      // so lan dung khoa hoi thoai

// ---- Kich ban tang tai: mo phong luong nguoi dung lon dan ----
export const options = {
  scenarios: {
    ramping_users: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: 5 },   // khoi dong: 5 user
        { duration: '40s', target: 5 },
        { duration: '20s', target: 15 },  // tang len 15 user
        { duration: '40s', target: 15 },
        { duration: '20s', target: 30 },  // cao diem: 30 user dong thoi
        { duration: '60s', target: 30 },
        { duration: '15s', target: 0 },   // ha tai
      ],
      gracefulStop: '30s',
    },
  },
  thresholds: {
    // Chi de tham chieu, khong lam dung test that bai
    ai_success_rate: ['rate>0.80'],
    ai_response_time: ['p(95)<120000'],
  },
};

// Cac cau hoi thuc te (mix don gian + tong hop) gui toi AI
const QUESTIONS = [
  'Co bao nhieu nguoi dung trong he thong?',
  'Co bao nhieu don vi trong bang units?',
  'Co bao nhieu spdv_tickets?',
  'Dem so nguoi dung theo trang thai',
  'Liet ke 5 don vi dau tien theo ten',
  'Co bao nhieu su kien trong spdv_ticket_events?',
];

export default function () {
  // conversation_id DUY NHAT cho moi request -> tranh khoa 409 theo hoi thoai
  const uid = `lt-${__VU}-${__ITER}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const question = QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)];

  const payload = JSON.stringify({
    message: question,
    conversation_id: uid,
    request_id: uid,
  });

  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: REQ_TIMEOUT,
    tags: { name: 'chat_sse' },
  };

  const res = http.post(ENDPOINT, payload, params);

  aiResponseTime.add(res.timings.duration);

  if (res.status === 409) {
    httpLock409.add(1);
  }

  // Thanh cong = HTTP 200 va stream ket thuc bang [DONE]
  const ok = res.status === 200 && String(res.body).includes('data: [DONE]');
  aiSuccessRate.add(ok);
  if (!ok) {
    aiErrors.add(1);
  }

  check(res, {
    'HTTP 200': (r) => r.status === 200,
    'stream hoan tat ([DONE])': (r) => String(r.body).includes('data: [DONE]'),
  });
}

// Xuat tom tat ra file JSON de bao cao
export function handleSummary(data) {
  return {
    stdout: textSummary(data),
    [__ENV.SUMMARY_OUT || 'k6_summary.json']: JSON.stringify(data, null, 2),
  };
}

// Tom tat gon gang khong can thu vien ngoai
function textSummary(data) {
  const m = data.metrics;
  const g = (name, sub) => {
    if (!m[name] || !m[name].values) return 'n/a';
    const v = m[name].values[sub];
    return v === undefined ? 'n/a' : Number(v).toFixed(2);
  };
  const lines = [];
  lines.push('================ KET QUA LOAD TEST AI ================');
  lines.push(`Tong so request:        ${g('http_reqs', 'count')}`);
  lines.push(`Request/giay (RPS):     ${g('http_reqs', 'rate')}`);
  lines.push(`Ty le thanh cong:       ${(Number(m.ai_success_rate ? m.ai_success_rate.values.rate : 0) * 100).toFixed(1)}%`);
  lines.push(`So loi:                 ${g('ai_errors', 'count')}`);
  lines.push(`Xung dot khoa (409):    ${g('lock_conflicts_409', 'count')}`);
  lines.push('-------- Thoi gian phan hoi AI (ms) --------');
  lines.push(`  trung binh (avg):     ${g('ai_response_time', 'avg')}`);
  lines.push(`  trung vi  (median):   ${g('ai_response_time', 'med')}`);
  lines.push(`  nhanh nhat (min):     ${g('ai_response_time', 'min')}`);
  lines.push(`  cham nhat (max):      ${g('ai_response_time', 'max')}`);
  lines.push(`  p(90):                ${g('ai_response_time', 'p(90)')}`);
  lines.push(`  p(95):                ${g('ai_response_time', 'p(95)')}`);
  lines.push('=====================================================');
  return lines.join('\n') + '\n';
}
