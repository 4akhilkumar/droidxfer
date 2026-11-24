// Notebook model shared by the phone pad and the Mac view. The server is the source of truth: clients
// apply the changes it broadcasts (leniently, like the server does for undo/redo) and never invent state.

export function newId() {
  const bytes = crypto.getRandomValues(new Uint8Array(9));
  return btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, (c) => ({ '+': 'a', '/': 'b', '=': '' })[c]);
}

const findPage = (state, id) => state.pages.find((p) => p.id === id);

function shift(obj, dx, dy) {
  if (obj.type === 'stroke') {
    for (const p of obj.pts) {
      p[0] += dx;
      p[1] += dy;
    }
  } else {
    obj.x += dx;
    obj.y += dy;
  }
}

const clone = (v) => structuredClone(v);

// Apply a change in place. Returns the ids of the pages it touched.
export function applyOp(state, op) {
  switch (op.op) {
    case 'add': {
      const page = findPage(state, op.page);
      if (!page) return [];
      op.objs.forEach((raw, i) => {
        if (page.objs[raw.id]) return;
        page.objs[raw.id] = clone(raw);
        const at = Array.isArray(op.at) && Number.isInteger(op.at[i]) ? op.at[i] : page.order.length;
        page.order.splice(Math.min(Math.max(at, 0), page.order.length), 0, raw.id);
      });
      return [page.id];
    }
    case 'remove': {
      const page = findPage(state, op.page);
      if (!page) return [];
      const gone = new Set(op.ids);
      for (const id of op.ids) delete page.objs[id];
      page.order = page.order.filter((id) => !gone.has(id));
      return [page.id];
    }
    case 'move': {
      const page = findPage(state, op.page);
      if (!page) return [];
      for (const id of op.ids) if (page.objs[id]) shift(page.objs[id], op.dx, op.dy);
      return [page.id];
    }
    case 'update': {
      const page = findPage(state, op.page);
      if (!page) return [];
      for (const [id, fields] of Object.entries(op.changes)) if (page.objs[id]) Object.assign(page.objs[id], fields);
      return [page.id];
    }
    case 'page_add': {
      if (findPage(state, op.page.id)) return [];
      const index = Number.isInteger(op.index) ? op.index : state.pages.length;
      state.pages.splice(Math.min(Math.max(index, 0), state.pages.length), 0, clone(op.page));
      return [op.page.id];
    }
    case 'page_remove': {
      if (state.pages.length <= 1) return [];
      state.pages = state.pages.filter((p) => p.id !== op.id);
      return [op.id];
    }
    case 'page_move': {
      const i = state.pages.findIndex((p) => p.id === op.id);
      if (i < 0) return [];
      const [page] = state.pages.splice(i, 1);
      state.pages.splice(Math.min(Math.max(op.index, 0), state.pages.length), 0, page);
      return [op.id];
    }
    case 'page_update': {
      const page = findPage(state, op.id);
      if (!page) return [];
      for (const key of ['template', 'bg', 'w', 'h']) if (key in op) page[key] = clone(op[key]);
      return [page.id];
    }
    case 'page_replace': {
      const i = state.pages.findIndex((p) => p.id === op.page.id);
      if (i < 0) state.pages.splice(Number.isInteger(op.index) ? op.index : state.pages.length, 0, clone(op.page));
      else state.pages[i] = clone(op.page);
      return [op.page.id];
    }
    case 'pages_replace':
      state.pages = clone(op.pages);
      return state.pages.map((p) => p.id);
    case 'batch':
      return [...new Set(op.ops.flatMap((sub) => applyOp(state, sub)))];
    default:
      return [];
  }
}

export const TOOLS = ['pen', 'pencil', 'highlighter'];
export const COLORS = ['#1d1d1f', '#e5484d', '#f76b15', '#ffc53d', '#30a46c', '#0a84ff', '#6e56cf', '#d6409f', '#8d6e63', '#ffffff'];
