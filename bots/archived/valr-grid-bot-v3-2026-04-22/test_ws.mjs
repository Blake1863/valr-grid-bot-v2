import WebSocket from 'ws';

const ws = new WebSocket('wss://api.valr.com/ws/trade');

ws.on('open', () => {
  console.log('Connected');
  const sub = {
    type: 'SUBSCRIBE',
    subscriptions: [
      { event: 'MARKET_SUMMARY_UPDATE', pairs: ['SOLUSDT'] },
    ]
  };
  ws.send(JSON.stringify(sub));
  console.log('Subscribed');
});

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  console.log('Full message:', JSON.stringify(msg, null, 2));
});

ws.on('error', (err) => console.error('Error:', err.message));
ws.on('close', (code) => console.log('Closed:', code));

setTimeout(() => { ws.close(); process.exit(0); }, 8000);
