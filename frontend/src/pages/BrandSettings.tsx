import { useState, useEffect } from 'react'
import { Palette, Save, Plus, X, Link as LinkIcon } from 'lucide-react'
import { toast } from 'sonner'
import { useBrand, useUpdateBrand } from '@/api/hooks'

export default function BrandSettings() {
  const { data: brand, isLoading } = useBrand()
  const update = useUpdateBrand()

  const [form, setForm] = useState({
    brand_name: '',
    description_template: '',
    color_palette: [] as string[],
    visual_style: '',
    motifs: [] as string[],
    genre_visual_modifiers: {} as Record<string, string>,
    title_format: '',
    youtube_playlists: {} as Record<string, string>,
    soundcloud_links: '',
    youtube_links: '',
  })

  const [newMotif, setNewMotif] = useState('')
  const [newColor, setNewColor] = useState('#7CB342')
  const [newModGenre, setNewModGenre] = useState('')
  const [newModStyle, setNewModStyle] = useState('')

  useEffect(() => {
    if (brand) {
      setForm({
        brand_name: brand.brand_name ?? '',
        description_template: brand.description_template ?? '',
        color_palette: brand.color_palette ?? [],
        visual_style: brand.visual_style ?? '',
        motifs: brand.motifs ?? [],
        genre_visual_modifiers: brand.genre_visual_modifiers ?? {},
        title_format: brand.title_format ?? '',
        youtube_playlists: brand.youtube_playlists ?? {},
        soundcloud_links: brand.soundcloud_links ?? '',
        youtube_links: brand.youtube_links ?? '',
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

  const addColor = () => {
    if (!newColor.trim()) return
    setForm({ ...form, color_palette: [...form.color_palette, newColor.trim()] })
    setNewColor('#7CB342')
  }

  const removeColor = (index: number) => {
    setForm({ ...form, color_palette: form.color_palette.filter((_, i) => i !== index) })
  }

  const addModifier = () => {
    if (!newModGenre.trim() || !newModStyle.trim()) return
    setForm({
      ...form,
      genre_visual_modifiers: { ...form.genre_visual_modifiers, [newModGenre.trim()]: newModStyle.trim() },
    })
    setNewModGenre('')
    setNewModStyle('')
  }

  const removeModifier = (genre: string) => {
    const mods = { ...form.genre_visual_modifiers }
    delete mods[genre]
    setForm({ ...form, genre_visual_modifiers: mods })
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

      {/* Brand Name */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Brand Name</h2>
        <input
          type="text"
          value={form.brand_name}
          onChange={(e) => setForm({ ...form, brand_name: e.target.value })}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-gray-200 focus:border-primary/40 focus:outline-none font-mono"
          placeholder="Your DJ name"
        />
      </div>

      {/* Color Palette */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Color Palette</h2>
        <div className="flex flex-wrap gap-3">
          {form.color_palette.map((color, i) => (
            <div key={i} className="flex items-center gap-2 group">
              <div
                className="w-10 h-10 rounded-lg border border-white/10"
                style={{ backgroundColor: color }}
              />
              <code className="text-xs text-gray-400 font-mono">{color}</code>
              <button
                onClick={() => removeColor(i)}
                className="p-1 text-gray-600 hover:text-cyber-red transition-colors opacity-0 group-hover:opacity-100"
              >
                <X className="w-3 h-3" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <input
            type="color"
            value={newColor}
            onChange={(e) => setNewColor(e.target.value)}
            className="w-10 h-10 rounded-lg cursor-pointer bg-transparent border-2 border-white/10 hover:border-primary/30 transition-colors"
          />
          <input
            type="text"
            value={newColor}
            onChange={(e) => setNewColor(e.target.value)}
            className="w-28 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 font-mono focus:border-primary/40 focus:outline-none"
          />
          <button
            onClick={addColor}
            className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 transition-all"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
        {/* Preview */}
        {form.color_palette.length > 0 && (
          <div className="mt-4 p-4 rounded-lg border border-white/5">
            <p className="text-[10px] text-gray-600 font-mono uppercase tracking-wider mb-3">Preview</p>
            <div className="flex gap-4 items-center">
              <div
                className="w-16 h-16 rounded-xl"
                style={{
                  background: `linear-gradient(135deg, ${form.color_palette[0] ?? '#7CB342'}, ${form.color_palette[1] ?? form.color_palette[0] ?? '#FF8A65'})`,
                }}
              />
              <div>
                <p className="text-lg font-bold" style={{ color: form.color_palette[0] ?? '#7CB342' }}>
                  {form.brand_name || 'Brand'}
                </p>
                <p className="text-sm" style={{ color: form.color_palette[1] ?? form.color_palette[0] ?? '#FF8A65' }}>
                  DJ Mix Series
                </p>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Visual Style */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Visual Style</h2>
        <p className="text-[11px] text-gray-600">Describe the visual aesthetic for AI-generated cover art</p>
        <textarea
          value={form.visual_style}
          onChange={(e) => setForm({ ...form, visual_style: e.target.value })}
          rows={4}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none resize-none font-mono"
          placeholder="e.g. Neon-soaked cyberpunk cityscapes with vibrant gradients and abstract geometric overlays..."
        />
      </div>

      {/* Title Format */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Title Format</h2>
        <p className="text-[11px] text-gray-600">
          Template for mix titles. Use {'{brand}'}, {'{genre}'}, {'{number}'} as variables.
        </p>
        <input
          type="text"
          value={form.title_format}
          onChange={(e) => setForm({ ...form, title_format: e.target.value })}
          className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
          placeholder="{brand} - {genre} Mix #{number}"
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

      {/* Genre Visual Modifiers */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400">Genre Visual Modifiers</h2>
        <p className="text-[11px] text-gray-600">Map genres to visual style overrides for cover art</p>
        <div className="space-y-2">
          {Object.entries(form.genre_visual_modifiers).map(([genre, style]) => (
            <div key={genre} className="flex items-center gap-2 group">
              <code className="w-32 shrink-0 px-3 py-2 bg-dark rounded-lg text-sm text-primary font-mono truncate">
                {genre}
              </code>
              <code className="flex-1 px-3 py-2 bg-dark rounded-lg text-sm text-gray-400 font-mono truncate">
                {style}
              </code>
              <button
                onClick={() => removeModifier(genre)}
                className="p-2 text-gray-600 hover:text-cyber-red transition-colors opacity-0 group-hover:opacity-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={newModGenre}
            onChange={(e) => setNewModGenre(e.target.value)}
            placeholder="Genre"
            className="w-32 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
          />
          <input
            type="text"
            value={newModStyle}
            onChange={(e) => setNewModStyle(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addModifier()}
            placeholder="Visual style modifier..."
            className="flex-1 px-3 py-2 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none font-mono"
          />
          <button
            onClick={addModifier}
            className="px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-lg text-sm hover:bg-primary/20 transition-all"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Links */}
      <div className="bg-surface-light border border-primary/10 rounded-xl p-6 space-y-5">
        <h2 className="text-sm font-mono text-gray-400 flex items-center gap-2">
          <LinkIcon className="w-4 h-4" /> Platform Links
        </h2>
        <div className="space-y-4">
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">SoundCloud Links</label>
            <textarea
              value={form.soundcloud_links}
              onChange={(e) => setForm({ ...form, soundcloud_links: e.target.value })}
              rows={3}
              className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none resize-none font-mono"
              placeholder="https://soundcloud.com/your-profile"
            />
          </div>
          <div className="space-y-2">
            <label className="text-[11px] text-gray-500 font-mono uppercase tracking-wider">YouTube Links</label>
            <textarea
              value={form.youtube_links}
              onChange={(e) => setForm({ ...form, youtube_links: e.target.value })}
              rows={3}
              className="w-full px-4 py-3 bg-dark border border-primary/10 rounded-lg text-sm text-gray-300 focus:border-primary/40 focus:outline-none resize-none font-mono"
              placeholder="https://youtube.com/@your-channel"
            />
          </div>
        </div>
      </div>
    </div>
  )
}
