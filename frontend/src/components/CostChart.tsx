import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from 'recharts'

interface CostSummaryItem {
  provider: string
  model: string
  operation: string
  total_cost: number
  count: number
}

interface CostChartProps {
  data: CostSummaryItem[]
}

export default function CostChart({ data }: CostChartProps) {
  if (!data.length) {
    return (
      <div className="h-64 flex items-center justify-center text-gray-600 text-sm font-mono">
        No cost data for this period
      </div>
    )
  }

  // Group by operation for the bar chart
  const operationMap = new Map<string, number>()
  for (const item of data) {
    const key = item.operation
    operationMap.set(key, (operationMap.get(key) ?? 0) + item.total_cost)
  }

  const chartData = Array.from(operationMap.entries()).map(([operation, cost]) => ({
    operation,
    cost,
  }))

  return (
    <ResponsiveContainer width="100%" height={280}>
      <BarChart data={chartData} margin={{ top: 5, right: 5, left: -10, bottom: 0 }}>
        <XAxis
          dataKey="operation"
          tick={{ fontSize: 10, fill: '#6b7280' }}
          axisLine={{ stroke: '#1f2937' }}
          tickLine={false}
        />
        <YAxis
          tick={{ fontSize: 10, fill: '#6b7280' }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(v: number) => `$${v.toFixed(2)}`}
        />
        <Tooltip
          contentStyle={{
            background: '#161B22',
            border: '1px solid rgba(124,179,66,0.3)',
            borderRadius: '8px',
            fontSize: '12px',
            fontFamily: '"JetBrains Mono", monospace',
          }}
          formatter={(value: number) => [`$${value.toFixed(4)}`, 'Cost']}
        />
        <Legend
          wrapperStyle={{ fontSize: '11px', fontFamily: '"JetBrains Mono", monospace' }}
        />
        <Bar dataKey="cost" fill="#7CB342" radius={[4, 4, 0, 0]} name="Cost" />
      </BarChart>
    </ResponsiveContainer>
  )
}
