import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import MixList from './pages/MixList'
import MixDetail from './pages/MixDetail'
import BrandSettings from './pages/BrandSettings'
import AppSettings from './pages/AppSettings'
import AIUsage from './pages/AIUsage'
import NotificationHistory from './pages/NotificationHistory'
import Upgrade from './pages/Upgrade'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/mixes" element={<MixList />} />
        <Route path="/mixes/:id" element={<MixDetail />} />
        <Route path="/brand" element={<BrandSettings />} />
        <Route path="/settings" element={<AppSettings />} />
        <Route path="/ai" element={<AIUsage />} />
        <Route path="/notifications" element={<NotificationHistory />} />
        <Route path="/upgrade" element={<Upgrade />} />
      </Routes>
    </Layout>
  )
}
