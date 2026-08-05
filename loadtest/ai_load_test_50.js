import { askRandomQuestion, textSummary } from './chat_probe.js';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const ENDPOINT = `${BASE_URL}/api/vanna/v2/chat_sse`;
const REQ_TIMEOUT = __ENV.REQ_TIMEOUT || '180s';

export const options = {
  scenarios: {
    peak_50_users: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '25s', target: 50 },   // tang nhanh len 50 user
        { duration: '120s', target: 50 },  // giu 50 user dong thoi
        { duration: '15s', target: 0 },    // ha tai
      ],
      gracefulStop: '60s',
    },
  },
  thresholds: {
    ai_success_rate: ['rate>0.70'],
    ai_response_time: ['p(95)<180000'],
  },
};

export default function () {
  askRandomQuestion(ENDPOINT, REQ_TIMEOUT, 'lt50');
}

export function handleSummary(data) {
  return {
    stdout: textSummary(data, 'KET QUA LOAD TEST AI - 50 USER'),
    [__ENV.SUMMARY_OUT || 'k6_summary_50.json']: JSON.stringify(data, null, 2),
  };
}
