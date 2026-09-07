function fetchCostForSub(id, cell, onFinish) {
    fetch("/api/sub/" + id + "/costs")
        .then(res => res.json())
        .then(data => {
            if (data.status === "Success") {
                const totalCost = Number(data.total_cost);
                const displayCost = Number.isFinite(totalCost) ? totalCost.toFixed(2) : '-';
                if (cell) cell.innerHTML = '<strong class="costs-amount">$' + displayCost + '</strong> <span class="costs-currency">' + escapeHtml(data.currency) + '</span>';
            } else if (data.status === "Warning") {
                if (cell) cell.innerHTML = '<span class="badge badge-warning" title="' + escapeHtml(data.message) + '">权限受限</span>';
            } else if (cell) {
                cell.innerHTML = '<span style="color: var(--text-muted);">-</span>';
            }
            if (typeof onFinish === "function") onFinish(data);
        })
        .catch(() => {
            if (cell) cell.innerHTML = '<span style="color: var(--danger);">查询失败</span>';
            if (typeof onFinish === "function") onFinish({ status: "Error", total_cost: 0 });
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
    if (totalEl) totalEl.textContent = "正在计算...";

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
            } else if (data.status !== "Warning") {
                hasError = true;
            }
            if (completedCount !== subIds.length) return;
            if (totalEl) {
                totalEl.innerHTML = '<span class="costs-amount">$' + grandTotal.toFixed(2) + '</span> <span class="costs-currency">' + escapeHtml(mainCurrency) + '</span>';
            }
            if (hasError) showTopAlert("部分账单读取失败，当前显示可用缓存。", "warning");
        });
    });
}

window.addEventListener("DOMContentLoaded", () => loadCostsData(false));
