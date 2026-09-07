/* Azure Manager shared dropdown controls.  This file intentionally uses only
 * browser primitives so it can run before any page-specific script. */
(function (window, document) {
    "use strict";

    const nativeControls = new WeakMap();
    let generatedId = 0;

    function all(selector, root = document) {
        return Array.from(root.querySelectorAll(selector));
    }

    function closeCustomDropdowns(except) {
        all('.custom-dropdown-container').forEach(container => {
            if (container !== except) {
                container.classList.remove('open');
                const button = container.querySelector('.custom-dropdown-btn');
                if (button) button.setAttribute('aria-expanded', 'false');
            }
        });
    }

    function closeNativeDropdowns(except) {
        all('.ui-native-dropdown').forEach(container => {
            if (container !== except) {
                container.classList.remove('open');
                const button = container.querySelector('.ui-native-dropdown-button');
                if (button) button.setAttribute('aria-expanded', 'false');
                const menu = container.querySelector('.ui-native-dropdown-menu');
                if (menu) menu.hidden = true;
                if (button) button.removeAttribute('aria-activedescendant');
            }
        });
    }

    function closeAll(except) {
        closeNativeDropdowns(except);
        closeCustomDropdowns(except);
    }

    function optionDisabled(option) {
        return option.disabled || (option.parentElement && option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled);
    }

    function customItemDisabled(item) {
        return item.classList.contains('disabled') || item.dataset.disabled === 'true';
    }

    function selectedOption(select) {
        return select.options[select.selectedIndex] || null;
    }

    function ensureSelectId(select) {
        if (!select.id) {
            generatedId += 1;
            select.id = `ui-select-${generatedId}`;
        }
        return select.id;
    }

    function positionMenu(container, button, menu) {
        const rect = button.getBoundingClientRect();
        const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 600;
        const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 800;
        const gap = 4;
        const edge = 8;
        const spaceBelow = Math.max(0, viewportHeight - rect.bottom - gap - edge);
        const spaceAbove = Math.max(0, rect.top - gap - edge);
        const forceDown = Boolean(container.closest('[data-dropdown-flow="down"]'));
        const opensBelow = forceDown || spaceBelow >= spaceAbove;
        let top = opensBelow ? rect.bottom + gap : rect.top - gap - Math.min(280, spaceAbove);
        top = Math.max(edge, Math.min(top, Math.max(edge, viewportHeight - edge)));
        const availableSpace = opensBelow ? viewportHeight - top - edge : rect.top - gap - top;
        const maxHeight = Math.max(1, Math.min(280, availableSpace));
        const width = Math.max(rect.width, 160);
        let left = rect.left;
        if (left + width > viewportWidth - 8) left = Math.max(8, viewportWidth - width - 8);
        menu.style.top = `${Math.max(8, top)}px`;
        menu.style.maxHeight = `${maxHeight}px`;
        menu.style.left = `${left}px`;
        menu.style.width = `${Math.min(width, viewportWidth - 16)}px`;
        menu.style.right = 'auto';
        menu.style.bottom = 'auto';
        container.style.setProperty('--ui-dropdown-width', `${rect.width}px`);
    }

    function scrollToSelected(menu, selector = '[role="option"].selected, .custom-dropdown-item.selected') {
        const item = menu && menu.querySelector(selector);
        if (!item || typeof item.scrollIntoView !== 'function') return;
        item.scrollIntoView({ block: 'nearest' });
    }

    function nativeItems(state) {
        return all('[role="option"]', state.menu).filter(item => item.getAttribute('aria-disabled') !== 'true');
    }

    function setNativeHighlight(state, item) {
        all('[role="option"]', state.menu).forEach(option => option.classList.toggle('highlighted', option === item));
        if (item) {
            state.button.focus();
            state.button.setAttribute('aria-activedescendant', item.id);
            scrollToSelected(item.parentElement, '[role="option"].highlighted');
        } else state.button.removeAttribute('aria-activedescendant');
    }

    function syncNativeState(state) {
        const { select, button, menu } = state;
        const selected = selectedOption(select);
        button.textContent = selected ? selected.textContent : '';
        if (!button.textContent.trim()) button.textContent = select.options.length ? '请选择' : '暂无可选项';
        button.disabled = select.disabled;
        button.setAttribute('aria-disabled', String(select.disabled));
        button.setAttribute('aria-expanded', String(state.container.classList.contains('open')));
        button.setAttribute('aria-required', String(select.required));
        all('[role="option"]', menu).forEach(item => {
            const selectedItem = selected && item.dataset.value === selected.value && item.dataset.index === String(select.selectedIndex);
            item.classList.toggle('selected', Boolean(selectedItem));
            item.setAttribute('aria-selected', String(Boolean(selectedItem)));
        });
        const active = menu.querySelector('[role="option"].selected:not([aria-disabled="true"])');
        if (state.container.classList.contains('open') && active) button.setAttribute('aria-activedescendant', active.id);
        else if (!state.container.classList.contains('open')) button.removeAttribute('aria-activedescendant');
    }

    function renderNativeOptions(state) {
        const { select, menu } = state;
        menu.replaceChildren();
        if (!select.options.length) {
            const empty = document.createElement('div');
            empty.className = 'ui-native-dropdown-empty';
            empty.textContent = '暂无可选项';
            menu.appendChild(empty);
            syncNativeState(state);
            if (state.container.classList.contains('open')) positionMenu(state.container, state.button, menu);
            return;
        }
        Array.from(select.children).forEach(child => {
            if (child.tagName === 'OPTGROUP') {
                const group = document.createElement('div');
                group.setAttribute('role', 'group');
                group.setAttribute('aria-label', child.label || '');
                const heading = document.createElement('div');
                heading.className = 'ui-native-dropdown-group';
                heading.textContent = child.label || '';
                group.appendChild(heading);
                Array.from(child.children).filter(option => option.tagName === 'OPTION').forEach(option => {
                    group.appendChild(createNativeOption(state, option, Array.from(select.options).indexOf(option), child.disabled));
                });
                menu.appendChild(group);
            } else if (child.tagName === 'OPTION') {
                menu.appendChild(createNativeOption(state, child, Array.from(select.options).indexOf(child), false));
            }
        });
        syncNativeState(state);
        if (state.container.classList.contains('open')) positionMenu(state.container, state.button, menu);
    }

    function createNativeOption(state, option, index, groupDisabled) {
        const item = document.createElement('div');
        item.setAttribute('role', 'option');
        item.id = `${state.id}-option-${index}`;
        item.dataset.dropdownOptionFor = state.id;
        item.dataset.value = option.value;
        item.dataset.index = String(index);
        item.textContent = option.textContent;
        const disabled = option.disabled || groupDisabled;
        item.setAttribute('aria-disabled', String(disabled));
        if (disabled) item.classList.add('disabled');
        item.addEventListener('click', () => {
            if (disabled) return;
            state.select.selectedIndex = index;
            state.select.value = option.value;
            state.select.dispatchEvent(new Event('change', { bubbles: true }));
            syncNativeState(state);
            closeNativeDropdowns();
        });
        return item;
    }

    function openNative(state) {
        if (state.select.disabled) return;
        renderNativeOptions(state);
        closeAll(state.container);
        state.container.classList.add('open');
        state.button.setAttribute('aria-expanded', 'true');
        state.menu.hidden = false;
        positionMenu(state.container, state.button, state.menu);
        const selectedItem = state.menu.querySelector('[role="option"].selected:not([aria-disabled="true"])');
        setNativeHighlight(state, selectedItem || nativeItems(state)[0]);
    }

    function toggleNative(state) {
        if (state.container.classList.contains('open')) closeNativeDropdowns();
        else openNative(state);
    }

    function handleNativeKeydown(state, event) {
        const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' ', 'Escape', 'Tab'];
        if (!keys.includes(event.key)) return;
        if (event.key === 'Escape' || event.key === 'Tab') {
            closeNativeDropdowns();
            return;
        }
        event.preventDefault();
        if (!state.container.classList.contains('open')) {
            openNative(state);
            if (event.key === 'Enter' || event.key === ' ') return;
        }
        const items = nativeItems(state);
        if (!items.length) return;
        const highlighted = state.menu.querySelector('[role="option"].highlighted');
        const selected = state.menu.querySelector('[role="option"].selected');
        const current = [highlighted, selected].find(item => item && item.getAttribute('aria-disabled') !== 'true') || null;
        let index = Math.max(0, items.indexOf(current));
        if (event.key === 'ArrowDown') index = Math.min(items.length - 1, index + 1);
        if (event.key === 'ArrowUp') index = Math.max(0, index - 1);
        if (event.key === 'Home') index = 0;
        if (event.key === 'End') index = items.length - 1;
        if (event.key === 'Enter' || event.key === ' ') {
            (current || items[index]).click();
            return;
        }
        setNativeHighlight(state, items[index]);
    }

    function enhanceSelect(select) {
        if (!(select instanceof window.HTMLSelectElement) || !select.classList.contains('form-control') || select.hasAttribute('data-custom-dropdown') || nativeControls.has(select)) return;
        const id = ensureSelectId(select);
        const container = document.createElement('div');
        container.className = 'ui-native-dropdown';
        container.dataset.uiDropdown = id;
        const isToolbar = select.classList.contains('toolbar-select');
        if (isToolbar) container.classList.add('toolbar-select-container');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'form-control ui-native-dropdown-button';
        if (isToolbar) button.classList.add('toolbar-select');
        button.dataset.dropdownFor = id;
        button.setAttribute('aria-haspopup', 'listbox');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', `${id}-menu`);
        const label = all('label').find(candidate => candidate.htmlFor === id) || select.parentElement?.querySelector('label');
        if (label) {
            if (!label.id) label.id = `${id}-label`;
            button.setAttribute('aria-labelledby', label.id);
            label.htmlFor = id;
        }
        const menu = document.createElement('div');
        menu.className = 'ui-native-dropdown-menu';
        menu.id = `${id}-menu`;
        menu.setAttribute('role', 'listbox');
        menu.hidden = true;
        container.append(button, menu);
        select.parentNode.insertBefore(container, select.nextSibling);
        select.classList.add('ui-native-select-source');
        select.setAttribute('aria-hidden', 'true');
        const state = { id, select, container, button, menu };
        nativeControls.set(select, state);
        select.tabIndex = -1;
        button.tabIndex = 0;
        button.addEventListener('click', event => { event.stopPropagation(); toggleNative(state); });
        button.addEventListener('keydown', event => handleNativeKeydown(state, event));
        select.addEventListener('change', () => syncNativeState(state));
        select.addEventListener('invalid', () => { button.focus(); }, true);
        const form = select.form;
        if (form) form.addEventListener('reset', () => window.setTimeout(() => { renderNativeOptions(state); }, 0));
        const observer = new MutationObserver(() => renderNativeOptions(state));
        observer.observe(select, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'selected', 'label', 'value'] });
        state.observer = observer;
        renderNativeOptions(state);
    }

    function refreshNativeSelects(root = document) {
        all('select.form-control', root).forEach(enhanceSelect);
        all('.ui-native-dropdown').forEach(container => {
            const selectId = container.dataset.uiDropdown;
            const select = document.getElementById(selectId);
            const state = select && nativeControls.get(select);
            if (state) syncNativeState(state);
        });
    }

    function decorateCustomDropdown(container) {
        const button = container.querySelector('.custom-dropdown-btn');
        const menu = container.querySelector('.custom-dropdown-menu');
        if (!button || !menu) return;
        if (!menu.id) menu.id = `${container.id || 'custom-dropdown'}-menu`;
        menu.setAttribute('role', 'listbox');
        button.setAttribute('role', 'button');
        button.tabIndex = 0;
        button.setAttribute('aria-controls', menu.id);
        if (button.dataset.uiDecorated === '1') {
            decorateCustomItems(container);
            return;
        }
        button.dataset.uiDecorated = '1';
        button.setAttribute('aria-haspopup', 'listbox');
        button.setAttribute('aria-expanded', container.classList.contains('open') ? 'true' : 'false');
        button.addEventListener('keydown', event => {
            const items = all('.custom-dropdown-item', container).filter(item => !customItemDisabled(item));
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' ', 'Escape', 'Tab'].includes(event.key)) return;
            if (event.key === 'Escape' || event.key === 'Tab') { closeCustomDropdowns(); return; }
            event.preventDefault();
            if (!container.classList.contains('open')) {
                closeAll(container);
                container.classList.add('open');
                button.setAttribute('aria-expanded', 'true');
                const menu = container.querySelector('.custom-dropdown-menu');
                if (menu) positionMenu(container, button, menu);
            }
            if (!items.length) return;
            const current = container.querySelector('.custom-dropdown-item.highlighted') || container.querySelector('.custom-dropdown-item.selected');
            let index = Math.max(0, items.indexOf(current));
            if (event.key === 'ArrowDown') index = Math.min(items.length - 1, index + 1);
            if (event.key === 'ArrowUp') index = Math.max(0, index - 1);
            if (event.key === 'Home') index = 0;
            if (event.key === 'End') index = items.length - 1;
            if (event.key === 'Enter' || event.key === ' ') { (current || items[index]).click(); return; }
            items.forEach(item => item.classList.toggle('highlighted', item === items[index]));
            items[index].scrollIntoView?.({ block: 'nearest' });
        });
        decorateCustomItems(container);
        if (!container.__uiDropdownObserver) {
            const observer = new MutationObserver(() => decorateCustomItems(container));
            observer.observe(container, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'aria-disabled'] });
            container.__uiDropdownObserver = observer;
        }
    }

    function decorateCustomItems(container) {
        const button = container.querySelector('.custom-dropdown-btn');
        all('.custom-dropdown-item', container).forEach(item => {
            item.setAttribute('role', 'option');
            const disabled = customItemDisabled(item);
            if (item.getAttribute('aria-disabled') !== String(disabled)) item.setAttribute('aria-disabled', String(disabled));
            if (item.dataset.uiItemDecorated === '1') return;
            item.dataset.uiItemDecorated = '1';
            item.addEventListener('click', () => {
                if (customItemDisabled(item)) return;
                all('.custom-dropdown-item', container).forEach(entry => {
                    entry.classList.toggle('selected', entry === item);
                    entry.classList.remove('highlighted');
                });
                button.setAttribute('aria-expanded', 'false');
            });
        });
    }

    function refreshCustomDropdowns() {
        all('.custom-dropdown-container').forEach(decorateCustomDropdown);
        positionOpenCustomMenus();
    }

    function syncDropdownAria() {
        all('.custom-dropdown-container').forEach(container => {
            const button = container.querySelector('.custom-dropdown-btn');
            if (button) button.setAttribute('aria-expanded', String(container.classList.contains('open')));
        });
        all('.ui-native-dropdown').forEach(container => {
            const button = container.querySelector('.ui-native-dropdown-button');
            if (button) button.setAttribute('aria-expanded', String(container.classList.contains('open')));
        });
    }

    function toggle(id, event) {
        if (event) event.stopPropagation();
        const container = typeof id === 'string' ? document.getElementById(id) : id;
        if (!container) return;
        const isOpen = container.classList.contains('open');
        closeAll(container);
        if (!isOpen) {
            decorateCustomDropdown(container);
            container.classList.add('open');
            const button = container.querySelector('.custom-dropdown-btn');
            if (button) button.setAttribute('aria-expanded', 'true');
            const menu = container.querySelector('.custom-dropdown-menu');
            if (menu) {
                positionMenu(container, button, menu);
                scrollToSelected(menu);
            }
        }
    }

    function positionOpenCustomMenus() {
        all('.custom-dropdown-container.open').forEach(container => {
            const button = container.querySelector('.custom-dropdown-btn');
            const menu = container.querySelector('.custom-dropdown-menu');
            if (!button || !menu) return;
            positionMenu(container, button, menu);
            button.setAttribute('aria-expanded', 'true');
        });
    }

    const api = {
        toggle,
        close: closeAll,
        refresh: () => { refreshNativeSelects(); refreshCustomDropdowns(); },
        scrollToSelected: (target) => {
            const container = typeof target === 'string' ? document.getElementById(target) : target;
            const menu = container && container.querySelector('.custom-dropdown-menu, .ui-native-dropdown-menu');
            if (menu) scrollToSelected(menu);
        }
    };
    window.AzureDropdown = api;

    document.addEventListener('mousedown', event => {
        if (!event.target.closest('.ui-native-dropdown, .custom-dropdown-container')) closeAll();
    });
    document.addEventListener('click', event => {
        if (event.target.closest('.custom-dropdown-container')) closeNativeDropdowns();
        syncDropdownAria();
        window.setTimeout(() => { syncDropdownAria(); positionOpenCustomMenus(); }, 0);
    }, true);
    document.addEventListener('click', () => positionOpenCustomMenus());
    document.addEventListener('keydown', event => { if (event.key === 'Escape') closeAll(); });
    function repositionOpenMenus() {
        all('.ui-native-dropdown.open').forEach(container => {
            const button = container.querySelector('button'); const menu = container.querySelector('[role="listbox"]');
            if (button && menu) positionMenu(container, button, menu);
        });
        positionOpenCustomMenus();
    }
    window.addEventListener('scroll', repositionOpenMenus, true);
    window.addEventListener('resize', repositionOpenMenus);

    function initialize() {
        refreshNativeSelects();
        refreshCustomDropdowns();
        all('.modal-overlay').forEach(overlay => overlay.addEventListener('click', event => {
            if (event.target === overlay) closeAll();
        }));
        const originalCloseModal = window.closeModal;
        if (typeof originalCloseModal === 'function' && !originalCloseModal.__uiDropdownWrapped) {
            const wrapped = function () { closeAll(); return originalCloseModal.apply(this, arguments); };
            wrapped.__uiDropdownWrapped = true;
            window.closeModal = wrapped;
        }
        new MutationObserver(records => {
            const relevant = records.some(record => {
                if (record.target.closest?.('.custom-dropdown-container')) return true;
                return Array.from(record.addedNodes).some(node => {
                    return node.nodeType === 1 && (node.matches?.('select.form-control, .custom-dropdown-container') || node.querySelector?.('select.form-control, .custom-dropdown-container'));
                });
            });
            if (relevant) { refreshNativeSelects(); refreshCustomDropdowns(); syncDropdownAria(); }
        }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, { once: true });
    else initialize();
})(window, document);
