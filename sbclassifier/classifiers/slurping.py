from email import message_from_string
import os
import re
import socket
try:
    import urllib.request as request
    from urllib.error import URLError
except ImportError:
    import urllib2 as request
    from urllib2 import URLError

import logging

from sbclassifier.classifiers.basic import Classifier
from sbclassifier.classifiers.constants import BASIC_HEADER_TOKENIZE
from sbclassifier.classifiers.constants import BASIC_HEADER_TOKENIZE_ONLY
from sbclassifier.classifiers.constants import HAM_CUTOFF
from sbclassifier.classifiers.constants import MAX_DISCRIMINATORS
from sbclassifier.classifiers.constants import SPAM_CUTOFF
from sbclassifier.classifiers.constants import USE_BIGRAMS
from sbclassifier.corpora.filesystem import ExpiryFileCorpus
from sbclassifier.corpora.filesystem import FileMessageFactory
from sbclassifier.safepickle import pickle_read
from sbclassifier.safepickle import pickle_write
from sbclassifier.tokenizer import Tokenizer
from sbclassifier.strippers import URLStripper

DOMAIN_AND_PORT_RE = re.compile(r"([^:/\\]+)(:([\d]+))?")
HTTP_ERROR_RE = re.compile(r"HTTP Error ([\d]+)")
URL_KEY_RE = re.compile(r"[\W]")

#: The username to give to the HTTP proxy when required.  If a username is
#: not necessary, simply leave blank.
PROXY_USERNAME = ''

#: The password to give to the HTTP proxy when required.  This is stored in
#: clear text in your configuration file, so if that bothers you then don't do
#: this. You'll need to use a proxy that doesn't need authentication, or do
#: without any SpamBayes HTTP activity.
PROXY_PASSWORD = ''

#: If a spambayes application needs to use HTTP, it will try to do so through
#: this proxy server. The port defaults to 8080, or can be entered with the
#: server:port form.
PROXY_SERVER = ''

#: (EXPERIMENTAL) This is the number of days that local cached copies of the
#: text at the URLs will be stored for.
X_CACHE_EXPIRY_DAYS = 7

#: (EXPERIMENTAL) So that SpamBayes doesn't need to retrieve the same URL over
#: and over again, it stores local copies of the text at the end of the URL.
#: This is the directory that will be used for those copies.
X_CACHE_DIRECTORY = 'url-cache'

#: (EXPERIMENTAL) To try and speed things up, and to avoid following unique
#: URLS, if this option is enabled, SpamBayes will convert the URL to as basic
#: a form it we can.  All directory information is removed and the domain is
#: reduced to the two (or three for those with a country TLD) top-most
#: elements. For example::
#:
#:     http://www.massey.ac.nz/~tameyer/index.html?you=me
#:
#: would become::
#:
#:     http://massey.ac.nz
#:
#: and::
#:
#:     http://id.example.com
#:
#: would become http://example.com
#:
#: This should have two beneficial effects:
#:  o It's unlikely that any information could be contained in this 'base'
#:    url that could identify the user (unless they have a *lot* of domains).
#:  o Many urls (both spam and ham) will strip down into the same 'base' url.
#:    Since we have a limited form of caching, this means that a lot fewer
#:    urls will have to be retrieved.
#: However, this does mean that if the 'base' url is hammy and the full is
#: spammy, or vice-versa, that the slurp will give back the wrong information.
#: Whether or not this is the case would have to be determined by testing.
X_ONLY_SLURP_BASE = False

#: (EXPERIMENTAL) It may be that what is hammy/spammy for you in email isn't
#: from webpages.  You can then set this option (to "web:", for example), and
#: effectively create an independent (sub)database for tokens derived from
#: parsing web pages.
X_WEB_PREFIX = ''

slurp_wordstream = None


class SlurpingURLStripper(URLStripper):
    def __init__(self):
        URLStripper.__init__(self)

    def analyze(self, text):
        # If there are no URLS, then we need to clear the
        # wordstream, or whatever was there from the last message
        # will be used.
        slurp_wordstream = None
        # Continue as normal.
        return URLStripper.analyze(self, text)

    def tokenize(self, m):
        # XXX Note that the 'slurped' tokens are *always* trained
        # XXX on; it would be simple to change/parameterize this.
        tokens = URLStripper.tokenize(self, m)
        # if not options["URLRetriever", "x-slurp_urls"]:
        #     return tokens

        proto, guts = m.groups()
        if proto != "http":
            return tokens

        assert guts
        while guts and guts[-1] in '.:;?!/)':
            guts = guts[:-1]

        slurp_wordstream = (proto, guts)
        return tokens


class SlurpingClassifier(Classifier):

    def spamprob(self, wordstream, evidence=False):
        """Do the standard chi-squared spamprob, but if the evidence
        leaves the score in the unsure range, and we have fewer tokens
        than max_discriminators, also generate tokens from the text
        obtained by following http URLs in the message."""
        h_cut = HAM_CUTOFF
        s_cut = SPAM_CUTOFF

        # Get the raw score.
        prob, clues = super().spamprob(wordstream, True)

        # If necessary, enhance it with the tokens from whatever is
        # at the URL's destination.
        if len(clues) < MAX_DISCRIMINATORS and \
           prob > h_cut and prob < s_cut and slurp_wordstream:
            slurp_tokens = list(self._generate_slurp())
            slurp_tokens.extend([w for (w, _p) in clues])
            sprob, sclues = super().spamprob(slurp_tokens, True)
            if sprob < h_cut or sprob > s_cut:
                prob = sprob
                clues = sclues
        if evidence:
            return prob, clues
        return prob

    def learn(self, wordstream, is_spam):
        """Teach the classifier by example.

        wordstream is a word stream representing a message.  If is_spam is
        True, you're telling the classifier this message is definitely spam,
        else that it's definitely not spam.
        """
        if USE_BIGRAMS:
            wordstream = self._enhance_wordstream(wordstream)
        wordstream = self._add_slurped(wordstream)
        self._add_msg(wordstream, is_spam)

    def unlearn(self, wordstream, is_spam):
        """In case of pilot error, call unlearn ASAP after screwing up.

        Pass the same arguments you passed to learn().
        """
        if USE_BIGRAMS:
            wordstream = self._enhance_wordstream(wordstream)
        wordstream = self._add_slurped(wordstream)
        self._remove_msg(wordstream, is_spam)

    def _generate_slurp(self):
        # We don't want to do this recursively and check URLs
        # on webpages, so we have this little cheat.
        if not hasattr(self, "setup_done"):
            self.setup()
            self.setup_done = True
        if not hasattr(self, "do_slurp") or self.do_slurp:
            if slurp_wordstream:
                self.do_slurp = False

                tokens = self.slurp(*slurp_wordstream)
                self.do_slurp = True
                self._save_caches()
                return tokens
        return []

    def setup(self):
        username = PROXY_USERNAME
        password = PROXY_PASSWORD
        server = PROXY_SERVER
        if server.find(":") != -1:
            server, port = server.split(':', 1)
        else:
            port = 8080
        if server:
            # Build a new opener that uses a proxy requiring authorization
            proxy_support = request.ProxyHandler(
                {"http": "http://%s:%s@%s:%d" %
                 (username, password,
                  server, port)})
            opener = request.build_opener(proxy_support, request.HTTPHandler)
        else:
            # Build a new opener without any proxy information.
            opener = request.build_opener(request.HTTPHandler)

        # Install it
        request.install_opener(opener)

        # Setup the cache for retrieved urls
        age = X_CACHE_EXPIRY_DAYS * 24 * 60 * 60
        dir = X_CACHE_DIRECTORY
        if not os.path.exists(dir):
            # Create the directory.
            logging.debug("Creating URL cache directory")
            os.makedirs(dir)

        self.urlCorpus = ExpiryFileCorpus(age, FileMessageFactory(),
                                          dir, cacheSize=20)
        # Kill any old information in the cache
        self.urlCorpus.removeExpiredMessages()

        # Setup caches for unretrievable urls
        self.bad_url_cache_name = os.path.join(dir, "bad_urls.pck")
        self.http_error_cache_name = os.path.join(dir, "http_error_urls.pck")
        if os.path.exists(self.bad_url_cache_name):
            try:
                self.bad_urls = pickle_read(self.bad_url_cache_name)
            except (IOError, ValueError):
                # Something went wrong loading it (bad pickle,
                # probably).  Start afresh.
                logging.warning("Bad URL pickle, using new.")
                self.bad_urls = {"url:non_resolving": (),
                                 "url:non_html": (),
                                 "url:unknown_error": ()}
        else:
            logging.debug("URL caches don't exist: creating")
            self.bad_urls = {"url:non_resolving": (),
                             "url:non_html": (),
                             "url:unknown_error": ()}
        if os.path.exists(self.http_error_cache_name):
            try:
                self.http_error_urls = pickle_read(self.http_error_cache_name)
            except (IOError, ValueError):
                # Something went wrong loading it (bad pickle,
                # probably).  Start afresh.
                logging.debug("Bad HHTP error pickle, using new.")
                self.http_error_urls = {}
        else:
            self.http_error_urls = {}

    def _save_caches(self):
        # XXX Note that these caches are never refreshed, which might not
        # XXX be a good thing long-term (if a previously invalid URL
        # XXX becomes valid, for example).
        for name, data in [(self.bad_url_cache_name, self.bad_urls),
                           (self.http_error_cache_name, self.http_error_urls),
                           ]:
            pickle_write(name, data)

    def slurp(self, proto, url):
        # We generate these tokens:
        #  url:non_resolving
        #  url:non_html
        #  url:http_XXX (for each type of http error encounted,
        #                for example 404, 403, ...)
        # And tokenise the received page (but we do not slurp this).
        # Actually, the special url: tokens barely showed up in my testing,
        # although I would have thought that they would more - this might
        # be due to an error, although they do turn up on occasion.  In
        # any case, we have to do the test, so generating an extra token
        # doesn't cost us anything apart from another entry in the db, and
        # it's only two entries, plus one for each type of http error
        # encountered, so it's pretty neglible.
        # If there is no content in the URL, then just return immediately.
        # "http://)" will trigger this.
        if not url:
            return ["url:non_resolving"]

        if X_ONLY_SLURP_BASE:
            url = self._base_url(url)

        # Check the unretrievable caches
        for err in list(self.bad_urls.keys()):
            if url in self.bad_urls[err]:
                return [err]
        if url in self.http_error_urls:
            return self.http_error_urls[url]

        # We check if the url will resolve first
        mo = DOMAIN_AND_PORT_RE.match(url)
        domain = mo.group(1)
        if mo.group(3) is None:
            port = 80
        else:
            port = mo.group(3)
        try:
            socket.getaddrinfo(domain, port)
        except socket.error:
            self.bad_urls["url:non_resolving"] += (url,)
            return ["url:non_resolving"]

        # If the message is in our cache, then we can just skip over
        # retrieving it from the network, and get it from there, instead.
        url_key = URL_KEY_RE.sub('_', url)
        cached_message = self.urlCorpus.get(url_key)

        if cached_message is None:
            # We're going to ignore everything that isn't text/html,
            # so we might as well not bother retrieving anything with
            # these extensions.
            parts = url.split('.')
            if parts[-1] in ('jpg', 'gif', 'png', 'css', 'js'):
                self.bad_urls["url:non_html"] += (url,)
                return ["url:non_html"]

            # Waiting for the default timeout period slows everything
            # down far too much, so try and reduce it for just this
            # call (this will only work with Python 2.3 and above).
            try:
                timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(5)
            except AttributeError:
                # Probably Python 2.2.
                pass
            try:
                logging.debug("Slurping %s", url)
                f = request.urlopen("%s://%s" % (proto, url))
            except (URLError, socket.error) as details:
                mo = HTTP_ERROR_RE.match(str(details))
                if mo:
                    self.http_error_urls[url] = "url:http_" + mo.group(1)
                    return ["url:http_" + mo.group(1)]
                self.bad_urls["url:unknown_error"] += (url,)
                return ["url:unknown_error"]
            # Restore the timeout
            try:
                socket.setdefaulttimeout(timeout)
            except AttributeError:
                # Probably Python 2.2.
                pass

            try:
                # Anything that isn't text/html is ignored
                content_type = f.info().get('content-type')
                if content_type is None or \
                   not content_type.startswith("text/html"):
                    self.bad_urls["url:non_html"] += (url,)
                    return ["url:non_html"]

                page = f.read()
                headers = str(f.info())
                f.close()
            except socket.error:
                # This is probably a temporary error, like a timeout.
                # For now, just bail out.
                return []

            fake_message_string = headers + "\r\n" + page

            # Retrieving the same messages over and over again will tire
            # us out, so we store them in our own wee cache.
            message = self.urlCorpus.makeMessage(url_key,
                                                 fake_message_string)
            self.urlCorpus.addMessage(message)
        else:
            fake_message_string = cached_message.as_string()

        msg = message_from_string(fake_message_string)

        # We don't want to do full header tokenising, as this is
        # optimised for messages, not webpages, so we just do the
        # basic stuff.
        bht = BASIC_HEADER_TOKENIZE
        bhto = BASIC_HEADER_TOKENIZE_ONLY

        BASIC_HEADER_TOKENIZE = True
        BASIC_HEADER_TOKENIZE_ONLY = True

        tokens = Tokenizer().tokenize(msg)
        pf = X_WEB_PREFIX
        tokens = ["%s%s" % (pf, tok) for tok in tokens]

        # Undo the changes
        BASIC_HEADER_TOKENIZE = bht
        BASIC_HEADER_TOKENIZE_ONLY = bhto
        return tokens

    def _base_url(self, url):
        # To try and speed things up, and to avoid following
        # unique URLS, we convert the URL to as basic a form
        # as we can - so http://www.massey.ac.nz/~tameyer/index.html?you=me
        # would become http://massey.ac.nz and http://id.example.com
        # would become http://example.com
        url += '/'
        domain = url.split('/', 1)[0]
        parts = domain.split('.')
        if len(parts) > 2:
            base_domain = parts[-2] + '.' + parts[-1]
            if len(parts[-1]) < 3:
                base_domain = parts[-3] + '.' + base_domain
        else:
            base_domain = domain
        return base_domain

    def _add_slurped(self, wordstream):
        """Add tokens generated by 'slurping' (i.e. tokenizing
        the text at the web pages pointed to by URLs in messages)
        to the wordstream."""
        for token in wordstream:
            yield token
        slurped_tokens = self._generate_slurp()
        for token in slurped_tokens:
            yield token
