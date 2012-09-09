#!/usr/bin/python

# To install dependencies:
# sudo apt-get install python-genshi python-gdata python-yaml

import collections, csv, os, re, shutil, yaml, sys
from genshi.template import MarkupTemplate, TextTemplate
from gdata.spreadsheet.service import SpreadsheetsService

# Our config, templates, etc. are installed next to this script
whereThisScriptLives = os.path.dirname(os.path.abspath(sys.modules['__main__'].__file__))
os.chdir(whereThisScriptLives)

# Load our config
with file('config.yml') as f:
	CONFIG = yaml.load(f.read())

# Work out exactly where to write our output
OUTDIR = CONFIG['output_directory']
if not os.path.isabs(OUTDIR): OUTDIR = os.path.join(whereThisScriptLives, OUTDIR)
OUTDIR = os.path.abspath(OUTDIR)  # resolve ../ to prevent doom in os.makedirs

def str2utf8(s):
	return unicode(s,'utf-8') if type(s) is str else s

def fixHTML(s):
	s = s or ''
	# Fix unclosed li tags (Genshi will close them all at once at the end of
	# the ul, but that's not ideal)
	s = re.sub(r'<li([^>]*)>([^<]+)(?=<li|</ul)', r'<li\1>\2</li>', s, flags=re.I)
	# Allow newlines
	s = re.sub(r'\s*\n\s*<(li|ul|/ul)([ >])', r'<\1\2', s)
	s = re.sub(r'</(ul|p|h1)>\s*\n\s*', r'</\1>', s)
	s = re.sub(r'\s*\n\s*', r'<br/>', s)
	# N.B. Genshi will also parse and re-emit this HTML
	return s

yesRE = re.compile(r'\s*y(?:es)?\s*', flags=re.I)
noRE  = re.compile(r'\s*no?\s*', flags=re.I)

def reverseAvailability(avail, verbose=False):
	parts = map(lambda p: p.rsplit(u':',1) if u':' in p else [u'',p], avail)
	if not verbose:
		if all(map(lambda p: noRE.match(p[1]),  parts)): return u'Y'
		if all(map(lambda p: yesRE.match(p[1]), parts)): return u'N'
		return u'part'
	yes = u'Available'
	no  = u'Not available'
	out = []
	for part in parts:
		if yesRE.match(part[1]): out.append(u'%s: %s' % (part[0], no) if part[0] else no)
		if noRE.match(part[1]):  out.append(u'%s: %s' % (part[0], yes) if part[0] else yes)
		# else ignore, fail safe, avoid accidentally disclosing names
	return out

# e.g. "A O some stuff here; U" -> [['A',None], ['O','some stuff here'], ['U',None]]
def parseTriggers(s):
	return re.findall(r'\b(U|A|D|SA|Ab|SI|O)\b(?:\s+(?!\b(?:U|A|D|SA|Ab|SI|O)\b)([^;]+);)?', s)

def expandTriggers(triggers):
	return map(lambda t: t[1] if t[0]==u'O' else u'%s: %s' % (CONFIG['trigger_warnings'][t[0]], t[1]), triggers)

def lastID(s):
	return s.id.text.split('/')[-1]

def whereAmI(cell):
	m = re.match(r'^R(\d+)C(\d+)$', lastID(cell))
	return int(m.group(1))-1, int(m.group(2))-1

def findByTitle(feed, title):
	for e in feed.entry:
		if e.title.text == title:
			return lastID(e)
	raise Exception, '%s had nothing with title "%s"' % (type(feed), title)

def cellsFeedToItems(feed, arrayName=None):
	# For ease of templating, item.foo always returns unicode, never None
	fieldNames, items, item = [], [], collections.defaultdict(unicode)
	rowOfPrevCell = 0
	# We want to save columns starting at arrayName as an array (the loan columns)
	arrayStartCol = -1

	for cell in feed.entry:
		row, col = whereAmI(cell)
		text = str2utf8(cell.content.text.strip())
		if row == 0:
			# Treat first row as headings
			if arrayStartCol < 0:  # i.e. not found array yet
				# Abbreviate field names to something Genshi-friendly
				# e.g. "Access notes (...)" -> "access"
				text = re.sub(r'\W+',' ',text)
				key = text.split(' ')[0].lower()
				if arrayName is not None and key == arrayName:
					arrayStartCol = col
				else:
					fieldNames.append(key)
		else:
			# We're not in the heading row
			if row != rowOfPrevCell:
				# We've finished with the previous row
				if item: yield item
				item = collections.defaultdict(unicode)
				rowOfPrevCell = row
			if arrayStartCol >= 0 and col >= arrayStartCol:
				# Skip odd-numbered columns after that
				if (col - arrayStartCol) % 2 == 0:
					# default new member would be unicode, so carefully create array instead
					if not arrayName in item: item[arrayName] = []
					item[arrayName].append(text)
			else:
				if col < len(fieldNames):
					item[fieldNames[col]] = text
				else:
					raise Exception, 'Content in unlabelled column at %d,%d: "%s"' % (row, col, text)
	# End of the cell list, so we've finished with the last row
	if item: yield item

# Find our spreadsheet and worksheets...
client = SpreadsheetsService()
client.ClientLogin(CONFIG['username'], CONFIG['password'])

sheetsFeed = client.GetSpreadsheetsFeed()
sheetID    = findByTitle(sheetsFeed, CONFIG['sheet_name'])

worksheets = client.GetWorksheetsFeed(sheetID)
catWS      = findByTitle(worksheets, CONFIG['catalogue_worksheet_name'])
textWS     = findByTitle(worksheets, CONFIG['text_worksheet_name'])

# Fetch custom text used by templates, e.g. the intro paragraph
TEXTS = {}
for item in cellsFeedToItems(client.GetCellsFeed(sheetID, textWS)):
	if not 'descriptor' in item: break
	TEXTS[item['descriptor']] = fixHTML(item['text'])

# Now parse the catalogue itself...
itemsBySection = collections.defaultdict(list)
sectionOrder   = []
filenamesUsed  = {}

for item in cellsFeedToItems(client.GetCellsFeed(sheetID, catWS), CONFIG['array_name']):
	filename, section = item.get('filename'), item.get('classification')
	if not filename or not section: continue

	# Prevent duplicate filenames
	if filename in filenamesUsed: raise Exception, 'Duplicate filename: "%s"' % filename
	filenamesUsed[filename] = 1

	# Resolve ambiguous HTML here, not on each visitor's browser
	item['description'] = fixHTML(item['description'])

	# More special cases...
	item['triggers'] = parseTriggers(item['trigger'])
	item['triggerLetters'] = map(lambda t: t[0], item['triggers'])
	item['triggersHTML'] = expandTriggers(item['triggers'])
	item['disabledLetters'] = re.sub(r'^([A-Z][A-Za-z]*).*', r'\1', item['disabled'])
	item['availableHTML'] = '<br/>'.join(reverseAvailability(item[CONFIG['array_name']]))
	item['availableHTMLFull'] = '<br/>'.join(reverseAvailability(item[CONFIG['array_name']], True))
	m = re.match(r'((?:[A-Z][A-Za-z]{1,4}:\s+[A-Za-z, ]+\.\s*)+)\s*(.*)', item['accessibility'])
	if m:
		item['accessibilityCodesYN'] = m.group(1)
		item['accessibilityCodes'] = ' '.join(re.findall(r'([A-Z][A-Za-z]{1,4}):\s+[Yy][^.]*\.', item['accessibilityCodesYN']))
		item['accessibilityNotes'] = m.group(2)

	# Gather sections by order of first appearance, put item in section
	if section not in itemsBySection: sectionOrder.append(section)
	itemsBySection[section].append(item)

# Actually, sort items within each section by title
for s in sectionOrder: itemsBySection[s].sort(key=lambda i: i['title'])

# Merge our section order & dict of sections into an ordered list of sections
sections = [(s,itemsBySection[s]) for s in sectionOrder]

# Load templates
with file('list.xml', 'r') as f: listTemplate  = MarkupTemplate(f)
with file('item.xml', 'r') as f: itemTemplate  = MarkupTemplate(f)
with file('title.xml','r') as f: titleTemplate = TextTemplate(f)

# Now let's write stuff, starting by nuking the output directory
try: shutil.rmtree(OUTDIR)
except OSError,e: pass
os.makedirs(OUTDIR)

# Write the index page
listHTML = listTemplate.generate(texts=TEXTS, sections=sections).render('xhtml')
with file(os.path.join(OUTDIR, 'index.html'), 'w') as f: f.write(listHTML)

# Write the item pages and title.txt files
for sectionName, items in itemsBySection.iteritems():
	for item in items:
		itemHTML  = itemTemplate.generate( texts=TEXTS, item=item).render('xhtml')
		titleText = titleTemplate.generate(texts=TEXTS, item=item).render('text')
		itemDir   = os.path.join(OUTDIR, item['filename'])
		os.mkdir(itemDir)
		with file(os.path.join(itemDir, 'index.html'), 'w') as f: f.write(itemHTML)
		with file(os.path.join(itemDir, 'title.txt' ), 'w') as f: f.write(titleText)
