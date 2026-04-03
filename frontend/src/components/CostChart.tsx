import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from 'recharts'

interface CostChartProps {
  data: { date: string; description: number; tracklist: number; tags: number; cover: number }[]
}

export default function CostChart({ data }: CostChartProps) {
  if (!data.length) {
    return (
      <div className="h-64 flex items-center justify-center text-gray-600 text-sm font-mono">
        No cost data for this period
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={280}>
      <BarChart data={data} margin={{ top: 5, right: 5, left: -10, bottom: 0 }}>
        <XAxis
          dataKey="date"
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
          formatter={(value: number, name: string) => [`$${value.toFixed(3)}`, name]}
        />
        <Legend
          wrapperStyle={{ fontSize: '11px', fontFamily: '"JetBrains Mono", monospace' }}
        />
        <Bar dataKey="description" stackId="a" fill="#7CB342" radius={[0, 0, 0, 0]} name="Description" />
        <Bar dataKey="tracklist" stackId="a" fill="#4FC3F7" name="Tracklist" />
        <Bar dataKey="tags" stackId="a" fill="#E040FB" name="Tags" />
        <Bar dataKey="cover" stackId="a" fill="#FF8A65" radius={[4, 4, 0, 0]} name="Cover Art" />
      </BarChart>
    </ResponsiveContainer>
  )
}
