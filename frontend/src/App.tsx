import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import MixList from './pages/MixList'
import MixDetail from './pages/MixDetail'
import BrandSettings from './pages/BrandSettings'
import AppSettings from './pages/AppSettings'
import AIUsage from './pages/AIUsage'
import NotificationsPage from './pages/NotificationsPage'
import ActivityPage from './pages/ActivityPage'
import Upgrade from './pages/Upgrade'
import CatalogPage from './pages/CatalogPage'
import CatalogMixEditor from './pages/CatalogMixEditor'
import ReviewQueue from './pages/ReviewQueue'
import ShortsPage from './pages/ShortsPage'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/mixes" element={<MixList />} />
        <Route path="/mixes/:id" element={<MixDetail />} />
        <Route path="/catalog" element={<CatalogPage />} />
        <Route path="/catalog/review" element={<ReviewQueue />} />
        <Route path="/catalog/:id" element={<CatalogMixEditor />} />
        <Route path="/shorts" element={<ShortsPage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/brand" element={<BrandSettings />} />
        <Route path="/settings" element={<AppSettings />} />
        <Route path="/ai" element={<AIUsage />} />
        <Route path="/notifications" element={<NotificationsPage />} />
        <Route path="/upgrade" element={<Upgrade />} />
      </Routes>
    </Layout>
  )
}
