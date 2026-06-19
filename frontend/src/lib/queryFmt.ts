// Shared MongoDB-shell rendering + JSON syntax highlighting for captured query
// entries. Used by the global Query Inspector and the underwriting operations
// popup so both render queries identically.

export interface ShellEntry {
	collection: string;
	operation: string;
	args: unknown;
}

function compactReplacer(_key: string, value: unknown): unknown {
	if (Array.isArray(value) && value.length > 4 && value.every((v) => typeof v === 'number')) {
		return [
			...value.slice(0, 3).map((v) => Math.round((v as number) * 10000) / 10000),
			`...${value.length} dims`
		];
	}
	return value;
}

export function fmt(obj: unknown): string {
	return JSON.stringify(obj, compactReplacer, 2);
}

export function toShell(entry: ShellEntry): string {
	const col = entry.collection;
	const a = entry.args as any;
	if (entry.operation === 'aggregate') return `db.${col}.aggregate(\n${fmt(a)}\n)`;
	if (entry.operation === 'find_one') {
		let s = `db.${col}.find_one(\n${fmt(a?.filter ?? a ?? {})}`;
		if (a?.projection) s += `,\n${fmt(a.projection)}`;
		return s + '\n)';
	}
	if (entry.operation === 'find') {
		let s = `db.${col}.find(\n${fmt(a?.filter ?? {})}`;
		if (a?.projection) s += `,\n${fmt(a.projection)}`;
		s += '\n)';
		if (a?.sort) s += `.sort(${fmt(a.sort)})`;
		if (a?.limit) s += `.limit(${a.limit})`;
		return s;
	}
	if (entry.operation === 'count_documents') return `db.${col}.count_documents(\n${fmt(a ?? {})}\n)`;
	return `db.${col}.${entry.operation}(\n${fmt(a)}\n)`;
}

export function highlight(code: string): string {
	const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
	const TOKEN =
		/(?<dims>"\.\.\.?\d+ dims?")|(?<more>"\.\.\.?\+\d+ (?:more|keys)")|(?<op>"\$\w+")|(?<key>"[\w_.]+?")(?=\s*:)|(?<=:\s*)(?<num>-?\d+\.?\d*(?:[eE][+-]?\d+)?)(?=[\s,\]\}\n])|(?<=:\s*)(?<str>"(?:[^"\\]|\\.)*")|(?<bool>\b(?:true|false|null)\b)|(?<db>^db\.[\w]+\.\w+)|(?<comment>\/\/[^\n]*)/gm;
	return esc.replace(TOKEN, (...args) => {
		const g = args[args.length - 1] as Record<string, string | undefined>;
		if (g.dims) return `<span class="tk-dim">${g.dims}</span>`;
		if (g.more) return `<span class="tk-dim">${g.more}</span>`;
		if (g.op) return `<span class="tk-op">${g.op}</span>`;
		if (g.key) return `<span class="tk-key">${g.key}</span>`;
		if (g.num !== undefined) return `<span class="tk-num">${g.num}</span>`;
		if (g.str) return `<span class="tk-str">${g.str}</span>`;
		if (g.bool) return `<span class="tk-bool">${g.bool}</span>`;
		if (g.db) return `<span class="tk-db">${g.db}</span>`;
		if (g.comment) return `<span class="tk-comment">${g.comment}</span>`;
		return args[0];
	});
}
