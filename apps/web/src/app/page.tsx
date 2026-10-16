"use client";

import React, { useState } from "react";
import {
  ShieldAlert,
  FileCheck2,
  Clock,
  Coins,
  BrainCircuit,
  ArrowUpRight,
  Filter,
  CheckCircle2,
  AlertTriangle,
  FileText,
  Search,
  ExternalLink,
} from "lucide-react";

interface ExceptionItem {
  id: string;
  poRef: string;
  supplier: string;
  type: string;
  owner: "Stores" | "Finance" | "Procurement";
  amountPkr: string;
  poPresent: boolean;
  grnPresent: boolean;
  invPresent: boolean;
  status: string;
  severity: "critical" | "warning";
}

const INITIAL_EXCEPTIONS: ExceptionItem[] = [
  {
    id: "EX-1048",
    poRef: "PO-44819",
    supplier: "Apex Logistics",
    type: "Short Delivery Invoiced (50 units)",
    owner: "Stores",
    amountPkr: "Rs. 125,000",
    poPresent: true,
    grnPresent: true,
    invPresent: true,
    status: "UNDER REVIEW",
    severity: "critical",
  },
  {
    id: "EX-1049",
    poRef: "PO-44820",
    supplier: "Indus Paper Co",
    type: "Unit Price Variance (+11.0%)",
    owner: "Finance",
    amountPkr: "Rs. 38,500",
    poPresent: true,
    grnPresent: true,
    invPresent: true,
    status: "NEW",
    severity: "warning",
  },
  {
    id: "EX-1050",
    poRef: "PO-44825",
    supplier: "Pak Fasteners",
    type: "Missing Goods Received Note (GRN)",
    owner: "Stores",
    amountPkr: "Rs. 210,000",
    poPresent: true,
    grnPresent: false,
    invPresent: true,
    status: "PENDING EVIDENCE",
    severity: "critical",
  },
  {
    id: "EX-1051",
    poRef: "PO-44831",
    supplier: "Crescent Steel",
    type: "Duplicate Invoice Detected",
    owner: "Finance",
    amountPkr: "Rs. 450,000",
    poPresent: true,
    grnPresent: true,
    invPresent: true,
    status: "UNDER REVIEW",
    severity: "critical",
  },
];

export default function DashboardPage() {
  const [selectedDept, setSelectedDept] = useState<string>("ALL");
  const [searchQuery, setSearchQuery] = useState<string>("");

  const filteredExceptions = INITIAL_EXCEPTIONS.filter((item) => {
    const matchesDept = selectedDept === "ALL" || item.owner.toUpperCase() === selectedDept;
    const matchesSearch =
      item.id.toLowerCase().includes(searchQuery.toLowerCase()) ||
      item.supplier.toLowerCase().includes(searchQuery.toLowerCase()) ||
      item.poRef.toLowerCase().includes(searchQuery.toLowerCase());
    return matchesDept && matchesSearch;
  });

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {/* Top Header */}
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/20">
              R
            </div>
            <div>
              <span className="font-bold tracking-wider text-lg text-white">RECORDA</span>
              <span className="ml-2 text-xs font-medium text-slate-400 bg-slate-800 px-2 py-0.5 rounded">
                Reconciliation & Exception Control
              </span>
            </div>
          </div>

          <div className="flex items-center gap-4 text-sm">
            <span className="text-slate-400">Org: <strong className="text-slate-200">Atlas Corp</strong></span>
            <div className="h-4 w-px bg-slate-800" />
            <div className="flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-xs text-emerald-400 font-medium">Reconciliation Engine Online</span>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        {/* Executive KPI Cards */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <div>
              <h1 className="text-2xl font-bold text-white">Executive Management Dashboard</h1>
              <p className="text-sm text-slate-400">Real-time capital at risk, exception bottlenecks, and classification health.</p>
            </div>
            <div className="text-xs bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-slate-300">
              Currency: <strong>PKR (Rs.)</strong>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* KPI 1 */}
            <div className="p-5 rounded-xl border border-slate-800 bg-slate-900/50 hover:border-slate-700 transition">
              <div className="flex items-center justify-between text-slate-400">
                <span className="text-xs font-semibold uppercase tracking-wider">Amount Under Review</span>
                <Coins className="w-5 h-5 text-amber-400" />
              </div>
              <div className="mt-2 flex items-baseline gap-2">
                <span className="text-2xl font-bold text-white">Rs. 2,450,000</span>
              </div>
              <p className="text-xs text-amber-400/90 mt-1 font-medium">
                12 Stores / 6 Finance pending
              </p>
            </div>

            {/* KPI 2 */}
            <div className="p-5 rounded-xl border border-slate-800 bg-slate-900/50 hover:border-slate-700 transition">
              <div className="flex items-center justify-between text-slate-400">
                <span className="text-xs font-semibold uppercase tracking-wider">Open Exceptions</span>
                <ShieldAlert className="w-5 h-5 text-rose-400" />
              </div>
              <div className="mt-2 flex items-baseline gap-2">
                <span className="text-2xl font-bold text-white">18 Cases</span>
              </div>
              <p className="text-xs text-rose-400/90 mt-1 font-medium">
                4 Critical Overbills requiring action
              </p>
            </div>

            {/* KPI 3 */}
            <div className="p-5 rounded-xl border border-slate-800 bg-slate-900/50 hover:border-slate-700 transition">
              <div className="flex items-center justify-between text-slate-400">
                <span className="text-xs font-semibold uppercase tracking-wider">Transactions Reconciled</span>
                <FileCheck2 className="w-5 h-5 text-emerald-400" />
              </div>
              <div className="mt-2 flex items-baseline gap-2">
                <span className="text-2xl font-bold text-white">1,420 Sets</span>
              </div>
              <p className="text-xs text-emerald-400/90 mt-1 font-medium">
                91.2% Clean without exceptions
              </p>
            </div>

            {/* KPI 4 */}
            <div className="p-5 rounded-xl border border-slate-800 bg-slate-900/50 hover:border-slate-700 transition">
              <div className="flex items-center justify-between text-slate-400">
                <span className="text-xs font-semibold uppercase tracking-wider">Avg Resolution Time</span>
                <Clock className="w-5 h-5 text-indigo-400" />
              </div>
              <div className="mt-2 flex items-baseline gap-2">
                <span className="text-2xl font-bold text-white">18.4 Hours</span>
              </div>
              <p className="text-xs text-indigo-400/90 mt-1 font-medium">
                -4.2 hrs vs prior month baseline
              </p>
            </div>
          </div>
        </section>

        {/* Classifier Health Banner */}
        <section className="p-5 rounded-xl border border-indigo-950 bg-indigo-950/20 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-lg bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
              <BrainCircuit className="w-6 h-6" />
            </div>
            <div>
              <h3 className="font-semibold text-white">ML Document Classifier Health</h3>
              <p className="text-xs text-slate-400">Trained model for document classification (PO, GRN, Supplier Invoice).</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-6 text-xs">
            <div>
              <span className="text-slate-400 block">Accuracy</span>
              <strong className="text-emerald-400 text-sm">96.4%</strong>
            </div>
            <div>
              <span className="text-slate-400 block">Precision</span>
              <strong className="text-slate-200 text-sm">95.8%</strong>
            </div>
            <div>
              <span className="text-slate-400 block">Recall</span>
              <strong className="text-slate-200 text-sm">97.1%</strong>
            </div>
            <div>
              <span className="text-slate-400 block">F1 Score</span>
              <strong className="text-indigo-400 text-sm">96.4%</strong>
            </div>
          </div>
        </section>

        {/* Exception Cases Table */}
        <section className="space-y-4">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
            <div>
              <h2 className="text-lg font-bold text-white">Active Exception Cases</h2>
              <p className="text-xs text-slate-400">Formal investigation workflows assigned to Stores, Finance, and Procurement.</p>
            </div>

            {/* Department Filter & Search */}
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
                <input
                  type="text"
                  placeholder="Search case, PO, supplier..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="bg-slate-900 border border-slate-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                />
              </div>

              <div className="flex rounded-lg border border-slate-800 bg-slate-900 p-0.5 text-xs">
                {["ALL", "STORES", "FINANCE", "PROCUREMENT"].map((dept) => (
                  <button
                    key={dept}
                    onClick={() => setSelectedDept(dept)}
                    className={`px-3 py-1 rounded-md font-medium transition ${
                      selectedDept === dept
                        ? "bg-indigo-600 text-white shadow"
                        : "text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {dept}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Table Container */}
          <div className="border border-slate-800 rounded-xl overflow-hidden bg-slate-900/40">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-slate-800 bg-slate-900/80 text-slate-400 uppercase tracking-wider font-semibold">
                  <th className="py-3 px-4">Case ID</th>
                  <th className="py-3 px-4">PO Ref</th>
                  <th className="py-3 px-4">Supplier</th>
                  <th className="py-3 px-4">Discrepancy / Type</th>
                  <th className="py-3 px-4">Owner</th>
                  <th className="py-3 px-4">Amount at Risk</th>
                  <th className="py-3 px-4">Evidence Checklist</th>
                  <th className="py-3 px-4 text-right">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 text-slate-300">
                {filteredExceptions.map((item) => (
                  <tr key={item.id} className="hover:bg-slate-800/30 transition">
                    <td className="py-3.5 px-4 font-mono font-bold text-white flex items-center gap-1.5">
                      {item.severity === "critical" ? (
                        <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />
                      ) : (
                        <span className="w-2 h-2 rounded-full bg-amber-400" />
                      )}
                      {item.id}
                    </td>
                    <td className="py-3.5 px-4 font-mono text-slate-400">{item.poRef}</td>
                    <td className="py-3.5 px-4 font-medium text-slate-200">{item.supplier}</td>
                    <td className="py-3.5 px-4 text-slate-300">{item.type}</td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold tracking-wide uppercase ${
                          item.owner === "Stores"
                            ? "bg-amber-950/60 text-amber-300 border border-amber-800/40"
                            : item.owner === "Finance"
                            ? "bg-emerald-950/60 text-emerald-300 border border-emerald-800/40"
                            : "bg-blue-950/60 text-blue-300 border border-blue-800/40"
                        }`}
                      >
                        {item.owner}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 font-mono font-semibold text-rose-300">{item.amountPkr}</td>
                    <td className="py-3.5 px-4">
                      <div className="flex items-center gap-1.5 font-mono text-[11px]">
                        <span
                          title="Purchase Order"
                          className={`px-1.5 py-0.5 rounded ${
                            item.poPresent ? "bg-indigo-950 text-indigo-300 border border-indigo-800/50" : "bg-slate-800 text-slate-500"
                          }`}
                        >
                          PO {item.poPresent ? "✓" : "✗"}
                        </span>
                        <span
                          title="Goods Received Note"
                          className={`px-1.5 py-0.5 rounded ${
                            item.grnPresent ? "bg-amber-950 text-amber-300 border border-amber-800/50" : "bg-rose-950/80 text-rose-400 border border-rose-800/50 font-bold"
                          }`}
                        >
                          GRN {item.grnPresent ? "✓" : "MISSING"}
                        </span>
                        <span
                          title="Supplier Invoice"
                          className={`px-1.5 py-0.5 rounded ${
                            item.invPresent ? "bg-purple-950 text-purple-300 border border-purple-800/50" : "bg-slate-800 text-slate-500"
                          }`}
                        >
                          INV {item.invPresent ? "✓" : "✗"}
                        </span>
                      </div>
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <button className="px-3 py-1 rounded bg-slate-800 hover:bg-indigo-600 text-slate-200 hover:text-white transition font-medium text-xs inline-flex items-center gap-1 shadow">
                        Resolve <ArrowUpRight className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </div>
  );
}
