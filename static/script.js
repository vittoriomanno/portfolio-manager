// Tab Navigation
const tabs = document.querySelectorAll('.nav-links a');
const panes = document.querySelectorAll('.tab-pane');

tabs.forEach(tab => {
    tab.addEventListener('click', (e) => {
        e.preventDefault();
        tabs.forEach(t => t.classList.remove('active'));
        panes.forEach(p => p.classList.remove('active'));

        tab.classList.add('active');
        const target = tab.getAttribute('data-tab');
        document.getElementById(`tab-${target}`).classList.add('active');
    });
});

// Chart.js Setup
const ctx = document.getElementById('equityChart').getContext('2d');

// LumaTrade gradient line graph look
let gradient = ctx.createLinearGradient(0, 0, 0, 400);
gradient.addColorStop(0, 'rgba(180, 212, 85, 0.5)'); // lime green transparent
gradient.addColorStop(1, 'rgba(180, 212, 85, 0.0)');

let equityChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: ['8:00', '9:30', '10:30', '11:30', '12:30', '13:30', '14:30', '15:30', '16:00'],
        datasets: [{
            label: 'Equity',
            data: [], // Filled dynamically or mocked
            borderColor: '#b4d455',
            backgroundColor: gradient,
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 6,
            pointHoverBackgroundColor: '#b4d455',
            fill: true,
            tension: 0.4 // smooth curves typical of clean dashboards
        }]
    },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
        scales: {
            x: { grid: { display: false, drawBorder: false }, ticks: { color: '#5d5d62', font: { family: 'Inter' } } },
            y: { grid: { color: '#2e2e32', borderDash: [5, 5] }, ticks: { color: '#5d5d62', font: { family: 'Inter' } } }
        },
        interaction: { mode: 'nearest', axis: 'x', intersect: false }
    }
});

function formatCurrency(num) {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num);
}

// Fetch Logic
async function fetchPortfolio() {
    try {
        const res = await fetch('/api/portfolio');
        const data = await res.json();

        document.getElementById('total-equity').textContent = formatCurrency(data.equity);
        document.getElementById('cash-available').textContent = formatCurrency(data.cash);
        document.getElementById('buying-power').textContent = formatCurrency(data.buying_power);
        document.getElementById('day-trades').innerHTML = `${3 - data.daytrade_count} <span class="unit">left</span>`;
        document.getElementById('pos-count').textContent = `${data.positions.length}/5`;

        // Mock chart data generation to simulate a heartbeat chart
        if (equityChart.data.datasets[0].data.length === 0) {
            let base = data.equity * 0.95;
            let arr = Array(9).fill(0).map((_, i) => base + (Math.random() * data.equity * 0.1));
            arr[8] = data.equity; // attach last point to current
            equityChart.data.datasets[0].data = arr;
            equityChart.update();
        }

        // Positions
        const tbody = document.querySelector('#positions-table tbody');
        tbody.innerHTML = '';
        if (data.positions.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: #5d5d62">No open positions</td></tr>';
        }

        data.positions.forEach(pos => {
            const tr = document.createElement('tr');
            const plClass = pos.pl_pct >= 0 ? 'positive' : 'negative';
            const plSign = pos.pl_pct >= 0 ? '+' : '';

            tr.innerHTML = `
                <td>
                    <div class="asset-cell">
                        <div class="asset-icon">${pos.symbol.charAt(0)}</div>
                        ${pos.symbol}
                    </div>
                </td>
                <td>${formatCurrency(pos.current)}</td>
                <td>${pos.qty}</td>
                <td><span class="change ${plClass}">${plSign}${pos.pl_pct.toFixed(2)}%</span></td>
                <td>${formatCurrency(pos.qty * pos.current)}</td>
            `;
            tbody.appendChild(tr);
        });

    } catch (e) {
        console.error("Failed to fetch portfolio", e);
    }
}

async function fetchLogs() {
    try {
        const res = await fetch('/api/log');
        const data = await res.json();
        const container = document.getElementById('decisions-log');
        container.innerHTML = '';

        data.forEach(log => {
            const div = document.createElement('div');
            let typeClass = log.type;
            if (log.msg.includes('BUY')) typeClass = 'buy';
            else if (log.msg.includes('SELL')) typeClass = 'sell';
            else if (log.msg.includes('HOLD')) typeClass = 'hold';

            div.className = `log-entry ${typeClass}`;
            div.innerHTML = `
                <div class="log-time">${log.time} | ${log.type.toUpperCase()}</div>
                <div class="log-msg">${log.msg}</div>
            `;
            container.appendChild(div);
        });
    } catch (e) { }
}

async function fetchAppLogs() {
    try {
        const res = await fetch('/api/applog');
        const data = await res.json();
        const container = document.getElementById('app-log');
        container.innerHTML = '';

        data.logs.forEach(line => {
            const div = document.createElement('div');
            let levelClass = '';
            if (line.includes('INFO')) levelClass = 'INFO';
            else if (line.includes('WARNING')) levelClass = 'WARNING';
            else if (line.includes('ERROR')) levelClass = 'ERROR';

            div.className = `line ${levelClass}`;
            div.textContent = line.trim();
            container.appendChild(div);
        });
    } catch (e) { }
}

// Init & Loops
fetchPortfolio();
fetchLogs();
fetchAppLogs();

setInterval(fetchPortfolio, 30000);
setInterval(fetchLogs, 30000);
setInterval(fetchAppLogs, 10000);

// Set real time session status visually
document.getElementById('session-text').textContent = 'Live tracking active';
