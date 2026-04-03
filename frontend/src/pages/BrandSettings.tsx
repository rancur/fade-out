import { useState, useEffect } from 'react'
import { Palette, Save, Plus, X, Link as LinkIcon } from 'lucide-react'
import { toast } from 'sonner'
import clsx from 'clsx'
import { useBrand, useUpdateBrand } from '@/api/hooks'

function ColorInput({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="space-y-2">
      <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">{label}</label>
      <div className="flex items-center gap-3">
        <div className="relative">
          <input
            type="color"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className="w-10 h-10 rounded-lg cursor-pointer bg-transparent border-2 border-white/10 hover:border-primary/30 transition-colors"
          />
        </div>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 font-mono focus:border-primary/40 focus:outline-none"
        />
        <div
          className="w-10 h-10 rounded-lg border border-white/10"
          style={{ backgroundColor: value }}
        />
      </div>
    </div>
  )
}

export default function BrandSettings() {
  const { data: brand, isLoading } = useBrand()
  const update = useUpdateBrand()

  const [form, setForm] = useState({
    artist_name: '',
    primary_color: '#7CB342',
    secondary_color: '#FF8A65',
    style_description: '',
    description_template: '',
    motifs: [] as string[],
    links: [] as { label: string; url: string }[],
  })

  const [newMotif, setNewMotif] = useState('')

  useEffect(() => {
    if (brand) {
      setForm({
        artist_name: brand.artist_name,
        primary_color: brand.primary_color,
        secondary_color: brand.secondary_color,
        style_description: brand.style_description,
        description_template: brand.description_template,
        motifs: brand.motifs,
        links: brand.links,
      })
    }
  }, [brand])

  const handleSave = () => {
    update.mutate(form, {
      onSuccess: () => toast.success('Brand settings saved'),
      onError: () => toast.error('Failed to save'),
    })
  }

  const addMotif = () => {
    if (!newMotif.trim()) return
    setForm({ ...form, motifs: [...form.motifs, newMotif.trim()] })
    setNewMotif('')
  }

  const removeMotif = (index: number) => {
    setForm({ ...form, motifs: form.motifs.filter((_, i) => i !== index) })
  }

  const addLink = () => {
    setForm({ ...form, links: [...form.links, { label: '', url: '' }] })
  }

  const updateLink = (index: number, field: 'label' | 'url', value: string) => {
    const links = [...form.links]
    links[index] = { ...links[index], [field]: value }
    setForm({ ...form, links })
  }

  const removeLink = (index: number) => {
    setForm({ ...form, links: form.links.filter((_, i) => i !== index) })
  }

  if (isLoading) {
    return <div className="h-96 bg-surface-light rounded-xl animate-pulse" />
  }

  return (
    <div className="space-y-8 max-w-3xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-pixel text-lg text-primary glow-text flex items-center gap-3">
            <Palette className="w-6 h-6" /> Brand
          </h1>
          <p className="text-sm text-gray-500 mt-1">Define your DJ brand identity for AI generation</p>
        </div>
        <button
          onClick={handleSave}
          disabled={update.isPending}
          className="flex items-center gap-2 px-5 py-2.5 bg-primary text-dark font-semibold rounded-lg text-sm hover:bg-primary/90 disabled:opacity-50 transition-all"
        >
          <Save className="w-4 h-4" />
          {update.isPending ? 'Saving...' : 'Save'}
        </button>
      </div>

      {/* Artist Name */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Artist Name</h2>
        <input
          type="text"
          value={form.artist_name}
          onChange={(e) => setForm({ ...form, artist_name: e.target.value })}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-gray-200 focus:border-primary/40 focus:outline-none font-mono"
          placeholder="Your DJ name"
        />
      </div>

      {/* Colors */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Brand Colors</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          <ColorInput
            label="Primary"
            value={form.primary_color}
            onChange={(v) => setForm({ ...form, primary_color: v })}
          />
          <ColorInput
            label="Secondary"
            value={form.secondary_color}
            onChange={(v) => setForm({ ...form, secondary_color: v })}
          />
        </div>
        {/* Preview */}
        <div className="mt-4 p-4 rounded-lg border border-white/5">
          <p className="text-[10px] text-gray-600 font-mono uppercase tracking-wider mb-3">Preview</p>
          <div className="flex gap-4 items-center">
            <div
              className="w-16 h-16 rounded-xl"
              style={{ background: `linear-gradient(135deg, ${form.primary_color}, ${form.secondary_color})` }}
            />
            <div>
              <p className="text-lg font-bold" style={{ color: form.primary_color }}>
                {form.artist_name || 'Artist'}
              </p>
              <p className="text-sm" style={{ color: form.secondary_color }}>
                DJ Mix Series
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* Style Description */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Style Description</h2>
        <p className="text-[11px] text-gray-600">Describe your DJ style for AI-generated descriptions</p>
        <textarea
          value={form.style_description}
          onChange={(e) => setForm({ ...form, style_description: e.target.value })}
          rows={4}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none resize-none font-mono"
          placeholder="e.g. Deep house / melodic techno DJ blending atmospheric soundscapes with driving rhythms..."
        />
      </div>

      {/* Description Template */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Description Template</h2>
        <p className="text-[11px] text-gray-600">
          Template for mix descriptions. Use {'{title}'}, {'{genres}'}, {'{tracklist}'}, {'{duration}'} as variables.
        </p>
        <textarea
          value={form.description_template}
          onChange={(e) => setForm({ ...form, description_template: e.target.value })}
          rows={8}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none resize-none font-mono"
          placeholder={`{title}\n\nGenre: {genres}\nDuration: {duration}\n\n{tracklist}\n\nFollow me on SoundCloud for more mixes.`}
        />
      </div>

      {/* Motifs */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Recurring Motifs / Keywords</h2>
        <div className="flex flex-wrap gap-2">
          {form.motifs.map((m, i) => (
            <span
              key={i}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-primary/10 text-primary border border-primary/20 rounded-full text-xs font-mono"
            >
              {m}
              <button onClick={() => removeMotif(i)} className="hover:text-cyber-red transition-colors">
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={newMotif}
            onChange={(e) => setNewMotif(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addMotif()}
            placeholder="Add a motif..."
            className="flex-1 px-4 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
          />
          <button
            onClick={addMotif}
            className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 transition-all"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Links */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
            <LinkIcon className="w-4 h-4" /> Social Links
          </h2>
          <button
            onClick={addLink}
            className="flex items-center gap-1.5 text-xs text-primary hover:text-primary/80 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" /> Add Link
          </button>
        </div>
        <div className="space-y-3">
          {form.links.map((link, i) => (
            <div key={i} className="flex gap-2">
              <input
                type="text"
                value={link.label}
                onChange={(e) => updateLink(i, 'label', e.target.value)}
                placeholder="Label"
                className="w-32 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              />
              <input
                type="url"
                value={link.url}
                onChange={(e) => updateLink(i, 'url', e.target.value)}
                placeholder="https://..."
                className="flex-1 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
              />
              <button
                onClick={() => removeLink(i)}
                className={clsx(
                  'p-2 rounded-lg border text-gray-500 hover:text-cyber-red hover:border-cyber-red/30 transition-all',
                  'border-white/5',
                )}
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
          {form.links.length === 0 && (
            <p className="text-sm text-gray-600 italic">No links added yet.</p>
          )}
        </div>
      </div>
    </div>
  )
}
