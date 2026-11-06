// Дерево деления рабочего стола: листья — окна, ветви — общие границы.
(() => {
    'use strict';
    function leaves(tree) {
        return !tree ? [] : tree.id ? [tree.id] : [...leaves(tree.first), ...leaves(tree.second)];
    }
    function sanitize(value, allowed, seen = new Set(), depth = 0) {
        if (!value || typeof value !== 'object' || depth > 12) return null;
        if (typeof value.id === 'string') {
            if (!allowed.has(value.id) || seen.has(value.id)) return null;
            seen.add(value.id);
            return { id: value.id };
        }
        if (value.axis !== 'x' && value.axis !== 'y') return null;
        const first = sanitize(value.first, allowed, seen, depth + 1);
        const second = sanitize(value.second, allowed, seen, depth + 1);
        if (!first || !second) return first || second;
        return { axis: value.axis, ratio: Number.isFinite(value.ratio) && value.ratio > 0 && value.ratio < 1 ? value.ratio : .5, first, second };
    }
    function prune(tree, visible) {
        if (!tree || tree.id) return tree && visible.has(tree.id) ? tree : null;
        tree.first = prune(tree.first, visible);
        tree.second = prune(tree.second, visible);
        return tree.first && tree.second ? tree : tree.first || tree.second;
    }
    function find(tree, id) {
        if (!tree || tree.id) return tree?.id === id ? tree : null;
        return find(tree.first, id) || find(tree.second, id);
    }
    function insert(tree, target, id, rect) {
        if (!tree) return { id };
        const leaf = find(tree, target) || find(tree, leaves(tree).at(-1));
        const previous = leaf.id;
        delete leaf.id;
        Object.assign(leaf, { axis: rect.w >= rect.h ? 'x' : 'y', ratio: .5, first: { id: previous }, second: { id } });
        return tree;
    }
    function layout(tree, rect, path = [], result = new Map()) {
        if (!tree) return result;
        if (tree.id) { result.set(tree.id, { rect: { ...rect }, path }); return result; }
        const horizontal = tree.axis === 'x';
        const size = horizontal ? rect.w : rect.h;
        const firstMinimum = minimum(tree.first, tree.axis), secondMinimum = minimum(tree.second, tree.axis);
        const scale = Math.min(1, size / (firstMinimum + secondMinimum));
        const split = Math.max(Math.round(firstMinimum * scale), Math.min(Math.round(size - secondMinimum * scale), Math.round(size * tree.ratio)));
        const first = { ...rect, [horizontal ? 'w' : 'h']: split };
        const second = { ...rect, [horizontal ? 'x' : 'y']: (horizontal ? rect.x : rect.y) + split, [horizontal ? 'w' : 'h']: size - split };
        layout(tree.first, first, [...path, { node: tree, rect, split, side: 'first' }], result);
        layout(tree.second, second, [...path, { node: tree, rect, split, side: 'second' }], result);
        return result;
    }
    function swap(tree, a, b) {
        const first = find(tree, a), second = find(tree, b);
        if (first && second) { first.id = b; second.id = a; }
    }
    function minimum(tree, axis) {
        if (tree.id) return axis === 'x' ? 100 : 72;
        const first = minimum(tree.first, axis), second = minimum(tree.second, axis);
        return tree.axis === axis ? first + second : Math.max(first, second);
    }
    function resize(branch, value, span) {
        const first = minimum(branch.first, branch.axis), second = minimum(branch.second, branch.axis);
        const scale = Math.min(1, span / (first + second));
        branch.ratio = Math.max(first * scale / span, Math.min(1 - second * scale / span, value));
    }
    window.DNOLayout = { leaves, sanitize, prune, insert, layout, swap, resize };
})();
