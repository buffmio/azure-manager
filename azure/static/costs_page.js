let costsPageAbortController = new AbortController();

function readCostsResponse(res) {
    return res.text().then(text => {
        let data = {};
        try { data = text ? JSON.parse(text) : {}; } catch (e) {
            data = { status: "Error", message: res.status === 401 ? "登录已过期，请重新登录" : `费用接口返回了无效响应（HTTP ${res.status}）` };
        }
        if (!res.ok && !data.message) data.message = `费用查询失败（HTTP ${res.status}）`;
        return data;
    });
}

function fetchCostForSub(id, cell, onFinish) {
    fetch("/api/sub/" + id + "/costs", { cache: "no-store", signal: costsPageAbortController.signal })
        .then(readCostsResponse)
        .then(data => {
            if (data.status === "Success") {
                const totalCost = Number(data.total_cost);
                const displayCost = Number.isFinite(totalCost) ? totalCost.toFixed(2) : '-';
                if (cell) cell.innerHTML = '<strong class="costs-amount">$' + displayCost + '</strong> <span class="costs-currency">' + escapeHtml(data.currency) + '</span>';
            } else if (data.status === "Warning") {
                if (cell) cell.innerHTML = '<span class="badge badge-warning" title="' + escapeHtml(data.message) + '">权限受限</span>';
            } else if (data.status === "Pending") {
                if (cell) {
                    const value = data.total_cost === null || data.total_cost === undefined ? '' : ('$' + Number(data.total_cost).toFixed(2) + ' ' + escapeHtml(data.currency || 'USD'));
                    cell.innerHTML = (value ? '<span class="costs-stale-value">' + value + '</span><br>' : '') + '<span class="badge badge-info" title="' + escapeHtml(data.message) + '">后台查询中</span>';
                }
            } else if (cell) {
                cell.innerHTML = '<span class="costs-error" title="' + escapeHtml(data.message || '费用查询失败') + '">' + escapeHtml(data.message || '查询失败') + '</span>';
            }
            if (data.status === "Pending" && data.task_id && typeof startWatchingTask === "function") {
                startWatchingTask("费用账单", "sync_costs", data.task_id, (success, task) => {
                    if (success) loadCostsData(false);
                    else showTopAlert("费用查询失败：" + ((task && (task.error_detail || task.progress_msg)) || "请稍后重试"), "danger");
                }, false);
            }
            if (typeof onFinish === "function") onFinish(data);
        })
        .catch(error => {
            if (error.name === "AbortError") return;
            if (cell) cell.innerHTML = '<span class="costs-error">网络连接失败，请稍后重试</span>';
            if (typeof onFinish === "function") onFinish({ status: "Error", total_cost: 0, message: error.message });
        });
}

function loadCostsData(forceRefresh = false) {
    const subIds = window.costTargetSubIds || [];
    const totalEl = document.getElementById("total-cost-display");
    const refreshBtn = document.getElementById("btn-refresh-cost");

    if (forceRefresh) {
        setCostRefreshBusy(true);
        showTopAlert("已开始刷新账单，进度可在活动通知查看。", "info");
        submitCostRefreshTasks(subIds, refreshBtn, () => loadCostsData(false));
        return;
    }

    let grandTotal = 0.0;
    let mainCurrency = "USD";
    let completedCount = 0;
    let hasError = false;
    let pendingCount = 0;
    if (totalEl) totalEl.textContent = "正在读取本地账单缓存...";

    if (!subIds.length) {
        if (totalEl) totalEl.textContent = "$0.00 USD";
        return;
    }

    subIds.forEach(id => {
        const cell = document.getElementById("sub-cost-" + id);
        if (cell) cell.innerHTML = '<span style="color: var(--text-muted); font-size: 0.85rem;">正在查询最新账单...</span>';
        fetchCostForSub(id, cell, data => {
            completedCount++;
            if (data.status === "Success") {
                grandTotal += parseFloat(data.total_cost || 0);
                mainCurrency = data.currency || "USD";
            } else if (data.status !== "Warning" && data.status !== "Pending") {
                hasError = true;
            }
            if (data.status === "Pending") pendingCount++;
            if (completedCount !== subIds.length) return;
            if (totalEl && completedCount === subIds.length) {
                if (pendingCount === subIds.length) {
                    totalEl.textContent = "费用正在后台查询...";
                    return;
                }
                totalEl.innerHTML = '<span class="costs-amount">$' + grandTotal.toFixed(2) + '</span> <span class="costs-currency">' + escapeHtml(mainCurrency) + '</span>';
            }
            if (hasError) showTopAlert("部分账单读取失败，当前显示可用缓存。", "warning");
        });
    });
}

window.addEventListener("DOMContentLoaded", () => loadCostsData(false));
window.addEventListener("pagehide", () => costsPageAbortController.abort(), { once: true });
