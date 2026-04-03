import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

interface EnergyChartProps {
  data: { time: number; energy: number }[]
}

export default function EnergyChart({ data }: EnergyChartProps) {
  if (!data.length) {
    return (
      <div className="h-48 flex items-center justify-center text-gray-600 text-sm font-mono">
        No energy data available
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 5, right: 5, left: -20, bottom: 0 }}>
        <defs>
          <linearGradient id="energyGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#7CB342" stopOpacity={0.4} />
            <stop offset="50%" stopColor="#00E5FF" stopOpacity={0.15} />
            <stop offset="95%" stopColor="#E040FB" stopOpacity={0} />
          </linearGradient>
          <linearGradient id="energyStroke" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#7CB342" />
            <stop offset="50%" stopColor="#00E5FF" />
            <stop offset="100%" stopColor="#E040FB" />
          </linearGradient>
        </defs>
        <XAxis
          dataKey="time"
          tick={{ fontSize: 10, fill: '#6b7280' }}
          tickFormatter={(v: number) => `${Math.floor(v / 60)}m`}
          axisLine={{ stroke: '#1f2937' }}
          tickLine={false}
        />
        <YAxis
          domain={[0, 1]}
          tick={{ fontSize: 10, fill: '#6b7280' }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
        />
        <Tooltip
          contentStyle={{
            background: '#161B22',
            border: '1px solid rgba(124,179,66,0.3)',
            borderRadius: '8px',
            fontSize: '12px',
            fontFamily: '"JetBrains Mono", monospace',
          }}
          formatter={(value: number) => [`${Math.round(value * 100)}%`, 'Energy']}
          labelFormatter={(label: number) => {
            const m = Math.floor(label / 60)
            const s = Math.floor(label % 60)
            return `${m}:${String(s).padStart(2, '0')}`
          }}
        />
        <Area
          type="monotone"
          dataKey="energy"
          stroke="url(#energyStroke)"
          strokeWidth={2}
          fill="url(#energyGrad)"
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
