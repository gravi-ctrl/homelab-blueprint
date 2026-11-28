-- ~/.config/nvim/lua/custom/restrictions.lua
-- Loaded from init.lua with: require('custom.restrictions')
-- Purpose: keep the tooling (LSP, fuzzy, completion) but force real Vim habits.

-- No mouse (kickstart sets 'a', this overrides it)
vim.o.mouse = ''

-- Relative numbers: use 5j, d3j, 12k instead of holding keys
vim.o.number = true
vim.o.relativenumber = true

-- Arrow keys disabled in normal, insert and visual mode
for _, key in ipairs { '<Up>', '<Down>', '<Left>', '<Right>' } do
  for _, mode in ipairs { 'n', 'i', 'v' } do
    vim.keymap.set(mode, key, '<Nop>', { desc = 'Use hjkl / motions' })
  end
end

-- Sensible indentation for DevOps files
vim.o.expandtab = true
vim.o.shiftwidth = 2
vim.o.tabstop = 2
vim.o.softtabstop = 2
vim.o.smartindent = true

-- Keep some context around the cursor while scrolling
vim.o.scrolloff = 8

-- Filetype detection so ansible-language-server attaches to playbooks.
-- Adjust the patterns to match your repo layout.
vim.filetype.add {
  pattern = {
    ['.*/playbooks/.*%.ya?ml'] = 'yaml.ansible',
    ['.*/roles/.*/tasks/.*%.ya?ml'] = 'yaml.ansible',
    ['.*/roles/.*/handlers/.*%.ya?ml'] = 'yaml.ansible',
    ['.*/playbook[^/]*%.ya?ml'] = 'yaml.ansible',
  },
}

-- Netrw (built-in file browser) tweaks, since there is no file tree plugin
vim.g.netrw_banner = 0
vim.g.netrw_liststyle = 3
