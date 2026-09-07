function setCostRefreshBusy(busy) {
    const btn = document.getElementById("btn-refresh-cost");
    setButtonBusy(btn, busy, "刷新中...", "刷新账单");
}

function checkAndRestoreCostTasks() {
    fetch("/api/tasks/list", { cache: "no-store" })
        .then(res => res.json())
        .then(data => {
            const active = (data.tasks || []).filter(t => t.task_type === "sync_costs" && (t.status === "InProgress" || t.status === "Pending"));
            if (!active.length) return;
            setCostRefreshBusy(true);
            active.forEach(t => startWatchingTask(t.target_name || "费用账单", t.task_type, t.id, function(success, task) {
                if (success) {
                    showTopAlert("账单刷新已完成。", "success");
                } else {
                    showTopAlert("账单刷新失败：" + ((task && (task.error_detail || task.progress_msg)) || "请稍后重试"), "danger");
                }
                setCostRefreshBusy(false);
                if (typeof loadCostsData === "function") loadCostsData(false);
            }, false));
        })
        .catch(err => console.error("Restore cost tasks error:", err));
}

window.addEventListener("DOMContentLoaded", checkAndRestoreCostTasks);

function submitCostRefreshTasks(subIds, refreshBtn, onComplete) {
    const ids = Array.isArray(subIds) ? subIds.slice() : [];
    let index = 0;
    let failed = false;

    const finish = () => {
        setCostRefreshBusy(false);
        if (failed) {
            showTopAlert("账单刷新完成，但部分订阅失败。", "warning");
        } else {
            showTopAlert("全部订阅账单已刷新完成。", "success");
        }
        if (typeof onComplete === "function") onComplete();
    };

    const submitNext = () => {
        if (index >= ids.length) {
            finish();
            return;
        }

        const id = ids[index++];
        fetch("/api/sub/" + id + "/costs_refresh_action", {
            method: "POST",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": window.csrfToken || ""
            }
        })
            .then(res => res.json().then(data => ({ ok: res.ok, data })))
            .then(({ ok, data }) => {
                const accepted = data.status === "submitted" || data.status === "in_progress";
                if (!ok || !accepted || !data.task_id) {
                    failed = true;
                    showTopAlert("账单刷新任务提交失败：" + (data.message || "请稍后重试"), "danger");
                    submitNext();
                    return;
                }
                startWatchingTask(data.target_name, data.task_type, data.task_id, (success, task) => {
                    if (!success) {
                        failed = true;
                        showTopAlert("账单刷新失败：" + (task.error_detail || task.progress_msg || "请稍后重试"), "danger");
                    }
                    submitNext();
                }, false);
            })
            .catch(error => {
                failed = true;
                showTopAlert("账单刷新任务提交失败：" + (error.message || "网络连接失败"), "danger");
                submitNext();
            });
    };

    if (!ids.length) {
        finish();
        return;
    }
    submitNext();
}
