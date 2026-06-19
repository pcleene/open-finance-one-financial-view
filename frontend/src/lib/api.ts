import { writable, get } from 'svelte/store';
import { browser } from '$app/environment';
import { queryLog } from '$lib/stores/queryLog.svelte';

// env-driven so the local UI can point at the deployed ALB; localhost defaults
export const API = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8010';
export const MOCK = import.meta.env.VITE_MOCK_URL ?? 'http://127.0.0.1:8100';

const stored = browser ? localStorage.getItem('ofv_customer') : null;
export const customer = writable<string>(stored || 'acme_cust_000001');
if (browser) customer.subscribe((v) => localStorage.setItem('ofv_customer', v));

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
	const r = await fetch(`${API}${path}`, init);
	if (!r.ok) {
		const body = await r.json().catch(() => ({}));
		throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, body });
	}
	const data = await r.json();
	// strip the captured MongoDB queries and feed the Query Inspector
	if (data && typeof data === 'object' && !Array.isArray(data)) {
		const queries = data._queries;
		delete data._queries;
		if (Array.isArray(queries) && queries.length) queryLog.push(queries, path);
	}
	return data as T;
}

export function cid(): string {
	return get(customer);
}

export const fmtRM = (v: string | number | null | undefined, dp = 2) => {
	if (v === null || v === undefined) return '—';
	const n = typeof v === 'string' ? parseFloat(v) : v;
	return new Intl.NumberFormat('en-MY', {
		minimumFractionDigits: dp,
		maximumFractionDigits: dp
	}).format(n);
};

export const fmtDate = (iso: string) =>
	new Date(iso).toLocaleDateString('en-MY', { day: 'numeric', month: 'short', year: 'numeric' });

export const fmtDateTime = (iso: string) =>
	new Date(iso).toLocaleString('en-MY', {
		day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
	});

export const INSTITUTION_COLORS: Record<string, string> = {
	'Acme-INTERNAL': '#EC0000',
	'DP-BANKB-001-7F3A': '#FFC600',
	'DP-BANKC-001-9C2B': '#0067B1',
	'DP-BANKD-001-4E1D': '#E31837',
	'DP-BANKE-001-8A5C': '#3D6BE5',
	'DP-NPF-001-2F9E': '#1E88E5'
};

export const SHORT_NAMES: Record<string, string> = {
	'Acme-INTERNAL': 'Acme',
	'DP-BANKB-001-7F3A': 'Bank Beta',
	'DP-BANKC-001-9C2B': 'Bank Gamma',
	'DP-BANKD-001-4E1D': 'Bank Delta',
	'DP-BANKE-001-8A5C': 'Bank Epsilon',
	'DP-NPF-001-2F9E': 'National Provident Fund'
};

export const CATEGORY_LABELS: Record<string, string> = {
	food_and_beverage: 'Food & Beverage',
	groceries: 'Groceries',
	transport: 'Transport',
	fuel: 'Fuel',
	shopping: 'Shopping',
	entertainment: 'Entertainment',
	bills_utilities: 'Bills & Utilities',
	telco_internet: 'Telco & Internet',
	health: 'Health',
	education: 'Education',
	travel: 'Travel',
	insurance: 'Insurance',
	financial_services: 'Financial Services',
	cash: 'Cash',
	transfers: 'Transfers',
	salary_income: 'Salary',
	investment: 'Investment',
	npf_contribution: 'NPF Contribution',
	loan_repayment: 'Loan Repayment',
	gambling: 'Gambling',
	uncategorized: 'Uncategorised'
};

export const CATEGORY_COLORS: Record<string, string> = {
	food_and_beverage: '#ff7849',
	groceries: '#7fb069',
	transport: '#5fa8d3',
	fuel: '#a78bfa',
	shopping: '#EC0000',
	entertainment: '#e85d75',
	bills_utilities: '#ffb627',
	telco_internet: '#48bfe3',
	health: '#56cfe1',
	education: '#80ffdb',
	travel: '#f4a261',
	insurance: '#94a3b8',
	financial_services: '#a3a380',
	cash: '#8d99ae',
	transfers: '#6c757d',
	salary_income: '#43aa8b',
	investment: '#90be6d',
	npf_contribution: '#577590',
	loan_repayment: '#bc4749',
	gambling: '#d90429',
	uncategorized: '#495057'
};
