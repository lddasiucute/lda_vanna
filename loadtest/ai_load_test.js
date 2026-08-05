import { askRandomQuestion, textSummary } from './chat_probe.js';

// ---- Cau hinh (co the override bang bien moi truong -e) ----
const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const ENDPOINT = `${BASE_URL}/api/vanna/v2/chat_sse`;
const REQ_TIMEOUT = __ENV.REQ_TIMEOUT || '180s';

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

export default function () {
  askRandomQuestion(ENDPOINT, REQ_TIMEOUT, 'lt');
}

// Xuat tom tat ra file JSON de bao cao
export function handleSummary(data) {
  return {
    stdout: textSummary(data, 'KET QUA LOAD TEST AI - 30 USER'),
    [__ENV.SUMMARY_OUT || 'k6_summary.json']: JSON.stringify(data, null, 2),
  };
}
