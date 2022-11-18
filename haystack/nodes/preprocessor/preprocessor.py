from typing import List, Optional, Set, Union, Tuple, Dict, Any

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal  # type: ignore

import logging
import re
from copy import deepcopy
from itertools import accumulate
import warnings
from pathlib import Path
from pickle import UnpicklingError

import nltk
from tqdm.auto import tqdm
from nltk.tokenize import NLTKWordTokenizer
from nltk.tokenize.api import TokenizerI

from haystack.nodes.preprocessor.base import BasePreProcessor
from haystack.schema import Document


logger = logging.getLogger(__name__)


REGEX_METACHARS = r".^$*+?{}[]\|()"


class PreProcessor(BasePreProcessor):
    def __init__(
        self,
        clean_whitespace: bool = True,
        clean_header_footer: bool = False,
        clean_empty_lines: bool = True,
        clean_substrings: List[str] = [],
        split_by: Literal["word", "sentence", "paragraph", "page", "regex", None] = "sentence",
        split_length: int = 5,
        split_overlap: int = 0,
        tokenizer_model_folder: Optional[Path] = None,
        language: str = "english",
        progress_bar: bool = True,
        add_page_number: bool = False,
        split_respect_sentence_boundary: bool = True,
    ):
        """
        :param clean_header_footer: Use heuristic to remove footers and headers across different pages by searching
                                    for the longest common string. This heuristic uses exact matches and therefore
                                    works well for footers like "Copyright 2019 by XXX", but won't detect "Page 3 of 4"
                                    or similar.
        :param clean_whitespace: Strip whitespaces before or after each line in the text.
        :param clean_empty_lines: Remove more than two empty lines in the text.
        :param clean_substrings: Remove specified substrings from the text.
        :param split_by: Unit for splitting the document. Can be "word", "sentence", or "passage". Set to None to disable splitting.
        :param split_length: Max. number of the above split unit (e.g. words) that are allowed in one document. For instance, if n -> 10 & split_by ->
                             "sentence", then each output document will have 10 sentences.
        :param split_overlap: Word overlap between two adjacent documents after a split.
                              Setting this to a positive number essentially enables the sliding window approach.
                              For example, if split_by -> `word`,
                              split_length -> 5 & split_overlap -> 2, then the splits would be like:
                              [w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12].
                              Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param language: The language used by "nltk.tokenize.sent_tokenize", for example "english", or "french".
                         Mind that some languages have limited support by the tokenizer: for example it seems incapable to split Chinese text
                         by word, but it can correctly split it by sentence.
        :param tokenizer_model_folder: Path to the folder containing the NTLK PunktSentenceTokenizer models, if loading a model from a local path.
                                       Leave empty otherwise.
        :param progress_bar: Whether to show a progress bar.
        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.
        :param split_respect_sentence_boundary: deprecated.
        """
        super().__init__()
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt")

        self.clean_whitespace = clean_whitespace
        self.clean_header_footer = clean_header_footer
        self.clean_empty_lines = clean_empty_lines
        self.clean_substrings = clean_substrings
        self.split_by = split_by
        self.split_length = split_length
        self.split_overlap = split_overlap
        self.language = language.lower()
        self.tokenizer_model_folder = tokenizer_model_folder
        self.progress_bar = progress_bar
        self.add_page_number = add_page_number

        if split_respect_sentence_boundary is not None:
            warnings.warn(
                "'split_respect_sentence_boundary' is deprecated. "
                "Setting 'split_by=\"word\"', sentence boundaries are never respected. "
                "Use 'split_by=\"sentence\"' to have the sentence boundaries respected. "
                "However, keep in mind that the 'split_length' will need to be adjusted, "
                "as it now refers to the number of chars.",
                DeprecationWarning,
            )
        self.split_respect_sentence_boundary = split_respect_sentence_boundary

    def process(
        self,
        documents: List[Document],
        clean_whitespace: Optional[bool] = None,
        clean_header_footer: Optional[bool] = None,
        clean_empty_lines: Optional[bool] = None,
        clean_substrings: List[str] = [],
        split_by: Literal["word", "sentence", "paragraph", "page", "regex"] = None,
        split_length: Optional[int] = None,
        split_overlap: Optional[int] = None,
        split_respect_sentence_boundary: Optional[bool] = None,
        add_page_number: bool = False,
    ) -> List[Document]:
        """
        Perform document cleaning and splitting.

        :param clean_header_footer: Use heuristic to remove footers and headers across different pages by searching
                                     for the longest common string. This heuristic uses exact matches and therefore
                                     works well for footers like "Copyright 2019 by XXX", but won't detect "Page 3 of 4"
                                     or similar.
        :param clean_whitespace: Strip whitespaces before or after each line in the text.
        :param clean_empty_lines: Remove more than two empty lines in the text.
        :param clean_substrings: Remove specified substrings from the text.
        :param split_by: Unit for splitting the document. Can be "word", "sentence", or "passage". Set to None to disable splitting.
        :param split_length: Max. number of the above split unit (e.g. words) that are allowed in one document. For instance, if n -> 10 & split_by ->
                           "sentence", then each output document will have 10 sentences.
        :param split_overlap: Word overlap between two adjacent documents after a split.
                              Setting this to a positive number essentially enables the sliding window approach.
                              For example, if split_by -> `word`,
                              split_length -> 5 & split_overlap -> 2, then the splits would be like:
                              [w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12].
                              Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param split_respect_sentence_boundary: deprecated
        :param language: The language used by "nltk.tokenize.sent_tokenize" in iso639 format.
            Available options: "ru","sl","es","sv","tr","cs","da","nl","en","et","fi","fr","de","el","it","no","pl","pt","ml"
        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.
        """
        if isinstance(documents, Document):
            warnings.warn(
                "Passing single documents to Preprocessor.process() is deprecated. Pass a list of Document objects",
                DeprecationWarning,
            )
            documents = [documents]

        elif isinstance(documents, dict):
            warnings.warn(
                "Passing dictionaries to Preprocessor.process() is deprecated. Pass a list of Document objects.",
                DeprecationWarning,
            )
            documents = [Document.from_dict(documents)]

        elif isinstance(documents, list) and isinstance(documents[0], dict):
            warnings.warn(
                "Passing dictionaries to Preprocessor.process() is deprecated. Pass a list of Document objects.",
                DeprecationWarning,
            )
            documents = [Document.from_dict(doc) for doc in documents]

        elif not isinstance(documents, list) or not isinstance(documents[0], Document):
            raise ValueError("'documents' must be a list of Document objects.")

        elif any(document.content_type != "text" for document in documents):
            ids = [doc.id for doc in documents if doc.content_type != "text"]
            raise ValueError(
                "Documents list contains one or more documents that are not of type 'text' "
                f"(doc ids: '{', '.join(ids)}'). Preprocessor only handles text documents."
            )

        if clean_whitespace is None:
            clean_whitespace = self.clean_whitespace
        if clean_header_footer is None:
            clean_header_footer = self.clean_header_footer
        if clean_empty_lines is None:
            clean_empty_lines = self.clean_empty_lines
        if not clean_substrings:
            clean_substrings = self.clean_substrings
        if split_by is None:
            split_by = self.split_by
        if split_length is None:
            split_length = self.split_length
        if split_overlap is None:
            split_overlap = self.split_overlap
        if split_respect_sentence_boundary is None:
            split_respect_sentence_boundary = self.split_respect_sentence_boundary

        cleaned_documents = [
            self.clean(
                document=doc,
                clean_whitespace=clean_whitespace,
                clean_header_footer=clean_header_footer,
                clean_empty_lines=clean_empty_lines,
                clean_substrings=clean_substrings,
            )
            for doc in documents
        ]
        if split_by:
            split_documents = [
                self.split(
                    document=doc,
                    split_by=split_by,
                    split_length=split_length,
                    split_overlap=split_overlap,
                    add_page_number=add_page_number,
                    split_respect_sentence_boundary=split_respect_sentence_boundary,
                )
                for doc in cleaned_documents
            ]
        return split_documents

    def process_batch(
        self,
        documents: List[Document],
        clean_whitespace: Optional[bool] = None,
        clean_header_footer: Optional[bool] = None,
        clean_empty_lines: Optional[bool] = None,
        clean_substrings: List[str] = [],
        split_by: Literal["word", "sentence", "passage", None] = None,
        split_length: Optional[int] = None,
        split_overlap: Optional[int] = None,
        split_respect_sentence_boundary: Optional[bool] = None,
        add_page_number: bool = False,
    ) -> List[Document]:
        nested_docs = [
            self.process(
                documents=docs,
                clean_whitespace=clean_whitespace,
                clean_header_footer=clean_header_footer,
                clean_empty_lines=clean_empty_lines,
                clean_substrings=clean_substrings,
                split_by=split_by,
                split_length=split_length,
                split_overlap=split_overlap,
                split_respect_sentence_boundary=split_respect_sentence_boundary,
                add_page_number=add_page_number,
            )
            for docs in tqdm(documents, disable=not self.progress_bar, desc="Preprocessing", unit="docs")
        ]
        return [d for x in nested_docs for d in x]

    def clean(
        self,
        document: Document,
        clean_whitespace: bool,
        clean_header_footer: bool,
        clean_empty_lines: bool,
        clean_substrings: Optional[List[str]] = None,
        clean_regex: Optional[str] = None,
        header_footer_n_chars: int = 50,
        header_footer_n_first_pages_to_ignore: int = 0,
        header_footer_n_last_pages_to_ignore: int = 0,
    ) -> Document:
        """
        Perform document cleaning on a single document and return a single document.
        This method will deal with whitespaces, headers, footers and empty lines.

        :param clean_whitespace: Strip whitespaces before or after each line in the text.
        :param clean_header_footer: Use heuristic to remove footers and headers across different pages by searching
                                     for the longest common string. This heuristic uses exact matches and therefore
                                     works well for footers like "Copyright 2019 by XXX", but won't detect "Page 3 of 4"
                                     or similar.
        :param clean_empty_lines: Remove more than two empty lines in the text.
        :param clean_substrings: Deprecated, use `clean_regex`
        :param clean_regex: Remove the specified regex matches from the text. For example, `clean_regex='[0-9]'`
                            removes all digits from the document's content, and `clean_regex='(a string|another string)'`
                            will remove all occurrences of either string from the document content.
        :param header_footer_n_chars: how many chars to look for headers and footer in.
        :param header_footer_n_first_pages_to_ignore: how many pages from the start to ignore in the header-footer detection heuristic
        :param header_footer_n_last_pages_to_ignore: how many pages from the end to ignore in the header-footer detection heuristic
        """
        if isinstance(document, dict):
            warnings.warn(
                "Passing a dictionary to Preprocessor.clean() is deprecated. Use Document objects.", DeprecationWarning
            )
            document = Document.from_dict(document)

        if clean_substrings:
            warnings.warn("clean_substrings is deprecated, use clean_regex", DeprecationWarning)
            clean_regex = f"({'|'.join(clean_substrings)})"

        if document.content_type != "text":
            raise ValueError(
                f"Document content type is not 'text', but '{document.content_type}'. Preprocessor only handles text documents."
            )

        clean_document = deepcopy(document)

        if clean_header_footer:
            clean_document = self.remove_header_footer(
                document=clean_document,
                n_chars=header_footer_n_chars,
                n_first_pages=header_footer_n_first_pages_to_ignore,
                n_last_pages=header_footer_n_last_pages_to_ignore,
            )

        if clean_whitespace:
            clean_document = self.remove_whitespace(document=clean_document)

        if clean_empty_lines:
            clean_document = self.remove_empty_lines(document=clean_document)

        if clean_regex:
            clean_document = self.remove_regex_matches(document=clean_document, regex=clean_regex)

        return clean_document

    def remove_header_footer(
        self,
        document: Document,
        n_chars: int = 100,
        n_first_pages: int = 0,
        n_last_pages: int = 0,
        min_len: int = 5,
        max_len: int = 50,
    ) -> Document:
        """
        Heuristic to find footers and headers across different pages by searching for the longest common prefix/suffix.
        For headers we only search in the first n_chars characters, for footers we search in the last n_chars.

        Note: This heuristic uses exact matches and therefore works well for footers like "Copyright 2019 by XXX",
        but won't detect "Page 3 of 4" or similar. For those, use `clean_regex`

        :param document: the document to remove headers and footers from.
        :param n_chars: number of first/last characters where the header/footer shall be searched in
        :param n_first_pages: number of first pages to ignore (e.g. TOCs often don't contain footer/header)
        :param n_first_pages: number of last pages to ignore
        :param min_len: how many chars, minimum, the header/footer can be made of
        :param max_len: how many chars, maximum, the header/footer can be made of
        """
        pages = [
            page for page in document.content.split("\f") if page.strip()
        ]  # empty pages are a typical issue for header/footer detection.
        relevant_pages = pages[n_first_pages:]
        if n_last_pages:
            relevant_pages = relevant_pages[:-n_last_pages]

        header = longest_common_prefix(
            texts=[page[:n_chars] for page in relevant_pages], min_len=min_len, max_len=max_len
        )
        if header:
            escaped_header = "".join([rf"\{char}" if char in REGEX_METACHARS else char for char in header])
            document = self.remove_regex_matches(document, regex=rf"{escaped_header}")
            logger.debug("Removed header: %s from doc id %s", header, document.id)

        footer = longest_common_suffix(
            texts=[page[-n_chars:] for page in relevant_pages], min_len=min_len, max_len=max_len
        )
        if footer:
            escaped_footer = "".join([rf"\{char}" if char in REGEX_METACHARS else char for char in footer])
            document = self.remove_regex_matches(document, regex=rf"{escaped_footer}")
            logger.debug("Removed footer: %s from doc id %s", footer, document.id)

        return document

    def remove_whitespace(self, document: Document) -> Document:
        """
        Strips leading and trailing whitespaces for each line in the text and
        re-aligns the headlines positions if they were present in the meta.

        :param document: the document to clean of whitespace
        :return: the document cleaned of whitespace, with the headlines positions re-aligned
                 if headlines were present in the meta.
        """
        pages = document.content.split("\f")
        clean_pages = []
        alignment_data = []

        for page in pages:
            clean_lines = []
            if not page:
                alignment_data.append((0, 1))  # Account for empty pages
            else:
                for line in page.splitlines():
                    clean_line = line.strip()
                    clean_lines.append(clean_line)
                    alignment_data.append(
                        (len(line) - len(clean_line), len(clean_line) + 1)
                    )  # +1: The \n we will append with .join()

            clean_pages.append("\n".join(clean_lines))
        document.content = "\f".join(clean_pages)

        if document.meta.get("headlines", None):
            document.meta["headlines"] = self._realign_headlines(
                headlines=document.meta["headlines"], alignment_data=alignment_data
            )

        return document

    def remove_empty_lines(self, document: Document) -> Document:
        """
        Remove empty lines and pages in the document and
        re-aligns the headlines positions if they were present in the meta.

        :param document: the document to clean of empty lines and empty pages
        :return: the document cleaned of empty lines, with the headlines positions re-aligned
                 if headlines were present in the meta.
        """
        pages = document.content.split("\f")
        clean_pages = []
        alignment_data = []

        for page in pages:
            clean_lines = []
            for line in page.splitlines():
                if line.strip():
                    clean_lines.append(line)
                    alignment_data.append((0, len(line) + 1))  # +1 The \n we will append with .join()
                else:
                    alignment_data.append((1, 0))  # +1 The \n we will append with .join()

            clean_pages.append("\n".join(clean_lines))
        document.content = "\f".join(clean_pages)

        if document.meta.get("headlines", None):
            document.meta["headlines"] = self._realign_headlines(
                headlines=document.meta["headlines"], alignment_data=alignment_data
            )

        return document

    def remove_regex_matches(self, document: Document, regex: str) -> Document:
        """
        Strips every match of the given regex in the text and
        re-aligns the headlines positions if they were present in the meta.

        :param document: the document to clean of whitespace
        :param substrings: the substrings to remove from the text
        :return: the document cleaned of whitespace, with the headlines positions re-aligned
                 if headlines were present in the meta.
        """
        # Empty regex patterns break this function
        if not regex.strip() or not regex.strip("()"):
            return document

        # If the regex matches nothing, just return
        matches = list(re.compile(regex).finditer(document.content))
        if not len(matches):
            return document

        # Find regex matches and save their start, end and lenght in three lists
        matches_start, matches_end, matches_sizes = zip(
            *((match.start(), match.end(), match.end() - match.start()) for match in matches)
        )
        # Use the lists above to compute the start-end position of the blocks of text to keep
        # plus the lenght of the string that matched the regex (to compute the offsets for the headline alignment)
        block_positions = zip([0, *matches_end], [*matches_start, len(document.content)], [*matches_sizes, 0])
        # Extract the blocks from the text and save the headline alignment data
        blocks = []
        alignment_data = []
        for block_start, block_end, separator_size in block_positions:
            block = document.content[block_start:block_end]
            blocks.append(block)
            alignment_data.append((separator_size, len(block)))

        alignment_data = alignment_data[:-1]  # The last point always refers to the end of the string and it's wrong
        document.content = "".join(blocks)

        # Must be done for each substring
        if document.meta.get("headlines", None):
            document.meta["headlines"] = self._realign_headlines(
                headlines=document.meta["headlines"], alignment_data=alignment_data
            )
        return document

    def _realign_headlines(self, headlines: List[Dict[str, Any]], alignment_data=List[Tuple[int, int]]):
        """
        Accessory for the whitespace/header/footer/empty lines removal functions.
        Keeps the headlines aligned after characters are removed from the document text.

        :param headlines: the content of document.meta["headlines"]
        :param alignment_data: tuple of (offset, clean_line_lenght) to track the shifts introduced by the
                               removal of the chars from the original document. These values are cumulative
                               and sorted.
        """
        headlines = sorted(headlines, key=lambda h: h["start_idx"])  # Necessary condition
        len_headlines = len(headlines)
        headline_to_shift = 0
        position_in_document = 0
        position_in_clean_document = 0

        for offset, clean_line_lenght in alignment_data:

            while position_in_document + offset + clean_line_lenght > headlines[headline_to_shift]["start_idx"]:
                headlines[headline_to_shift]["start_idx"] = position_in_clean_document + (
                    headlines[headline_to_shift]["start_idx"] - position_in_document
                )
                headline_to_shift += 1
                if headline_to_shift >= len_headlines:
                    return headlines

            position_in_document += offset + clean_line_lenght
            position_in_clean_document += clean_line_lenght

        for remaining_headline in range(headline_to_shift, len_headlines):
            headlines[remaining_headline]["start_idx"] = position_in_clean_document + (
                headlines[remaining_headline]["start_idx"] - position_in_document
            )
        return headlines

    def split(
        self,
        document: Document,
        split_by: Literal["word", "sentence", "paragraph", "page", "regex"],
        split_regex: Optional[str] = None,
        split_length: int = 1,
        split_overlap: int = 0,
        split_max_chars: int = 2000,
        split_respect_sentence_boundary: Optional[bool] = None,
        add_page_number=None,
    ) -> List[Document]:
        """
        Perform document splitting on a document. This method can split on different units, at different lengths,
        and include some overlap across the splits. It can also properly assign page numbers and re-assign headlines
        found in the metadata to each split document.

        No char should be lost in splitting, not even whitespace, and all headlines should be preserved.
        However, parts of the text and some headlines will be duplicated if `split_overlap > 0`.

        :param split_by: Unit for splitting the document. Can be "word", "sentence", "paragraph", "page" or "regex".
        :param split_regex: if split_by="regex", provide here a regex matching the separator. For example if the document
                            should be split on "--my separator--", this field should be `splitter="--my separator--"`
        :param split_length: Max. number of units (words, sentences, paragraph or pages, according to split_by)
                             that are allowed in one document.
        :param split_overlap: Unit overlap between two adjacent documents after a split.
                              Setting this to a positive number essentially enables the sliding window approach.
                              Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param split_max_chars: Absolute maximum number of chars allowed in a single document. Reaching this boundary
                                will cut the document, even mid-word, and log a loud error.\n
                                It's recommended to set this value approximately double double the size expect your documents
                                to be. For example, with `split_by='sentence'`, `split_lenght=2`, if the average sentence
                                length of our document is 100 chars, you should set `max_char=400` or `max_char=500`.\n
                                This is a safety parameter to avoid extremely long documents to end up in the document store.
                                Keep in mind that huge documents (tens of thousands of chars) will strongly impact the
                                performance of Reader nodes and might slow down drastically the indexing speed.
        :param add_page_number: Saves in the metadata ('page' key) the page number where the document content comes from.
        :param split_respect_sentence_boundary: deprecated, use `split_by='sentence'`.
        """
        if isinstance(document, dict):
            warnings.warn(
                "Calling Preprocessor.split() with dictionaries is deprecated. Use Document objects.",
                DeprecationWarning,
            )
            document = Document.from_dict(document)

        if split_respect_sentence_boundary is not None:
            warnings.warn(
                "'split_respect_sentence_boundary' is deprecated. "
                "Setting 'split_by=\"word\"', sentence boundaries are never respected. "
                "Use 'split_by=\"sentence\"' to have the sentence boundaries respected. "
                "However, keep in mind that the 'split_length' will need to be adjusted, "
                "as it now refers to the number of chars.",
                DeprecationWarning,
            )

        if split_overlap >= split_length:
            raise ValueError(f"split_length ({split_length}) must be greater than split_overlap ({split_overlap})")

        if document.content_type != "text":
            raise ValueError(
                f"Document content type is not 'text', but '{document.content_type}'. Preprocessor only handles text documents."
            )

        if split_by == "regex" and not split_regex:
            raise ValueError("If 'split_by' is set to 'regex', you must give a value to 'split_regex'.")

        if split_by == "regex":
            units = self.split_by_regex(text=document.content, splitter=split_regex)

        elif split_by == "page":
            units = self.split_by_regex(text=document.content, splitter="\f")

        elif split_by == "paragraph":
            units = self.split_by_regex(text=document.content, splitter="\n\n")

        elif split_by == "sentence":
            units = self.split_by_tokenizer(
                text=document.content,
                tokenizer=load_tokenizer(
                    language_name=self.language, tokenizer_model_folder=self.tokenizer_model_folder
                ),
            )

        elif split_by == "word":
            units = self.split_by_tokenizer(text=document.content, tokenizer=NLTKWordTokenizer())

        else:
            raise ValueError("split_by must be either word, sentence, paragraph, page or regex")

        # Create the groups according to split_lenght and split_overlap
        positions = [0] + list(accumulate([len(unit) for unit in units]))
        splits = [
            (
                "".join(units[pos : pos + split_length]),  # The split's text
                positions[pos],  # The split's starting character position in the source document
            )
            for pos in range(0, max(1, len(units) - split_overlap), split_length - split_overlap)
        ]

        # Headlines MUST be sorted by start_idx
        if document.meta.get("headlines"):
            document.meta["headlines"] = sorted(document.meta["headlines"], key=lambda h: h["start_idx"])

        split_documents = self._split_document(
            document=document, splits=splits, max_chars=split_max_chars, add_page_number=add_page_number
        )
        return split_documents

    def _split_document(
        self, document: Document, splits: List[Tuple[str, int]], max_chars: int, add_page_number: bool = True
    ) -> List[Document]:
        """
        Accessory function that splits a large document into the given chunks.
        Deals with too long chunks by splitting them over max_chars.
        Takes care of calculating page numbers and including only the relevant headlines into each smaller document.

        :param document: the original document the chunks come from. Needed to clone things such as id_hash_keys
        :param splits: a list of tuple containing the chunks of text this document will be split into and their
                       original position in the original document's content.
        :param max_chars: the maximum lenght in chars that the new small documents will have. If surpassed, the
                          affected document will be hard-split on the last char and the function will log loudly.
        :param add_page_number: whether to compute the page number for the new documents.
        :raturn: a list of small documents, each never longer than max_chars.
        """
        headlines = deepcopy(document.meta.get("headlines", []))
        split_docs = []
        page_index = 0

        # Page number must be tracked separately due to the split_overlap
        if add_page_number:
            page_start_positions = [pos for pos, char in enumerate(document.content) if char == "\f"]
            if page_start_positions and page_start_positions[-1] != len(document.content):
                page_start_positions.append(len(document.content))

        for split, position_in_document in splits:

            # # No empty documents
            # if not split.strip():
            #     continue

            split_doc = Document(content=split, meta=deepcopy(document.meta), id_hash_keys=document.id_hash_keys)

            # See how many pages we crossed in this chunk
            if add_page_number:
                while (
                    document.meta.get("page", 1) + page_index - 1 < len(page_start_positions)
                    and page_start_positions[page_index] < position_in_document + 1
                ):
                    page_index += 1
                split_doc.meta["page"] = document.meta.get("page", 1) + page_index

            # Find all the headlines starting in this chunk
            # NOTE: We assume that if a headline starts in a chunk it's completely included in it, but we don't check for that.
            split_doc_headlines = []
            while headlines and headlines[0]["start_idx"] < position_in_document:
                headlines = headlines[1:]
            for headline in headlines:
                if headline["start_idx"] < position_in_document + len(split):
                    split_doc_headlines.append({**headline, "start_idx": headline["start_idx"] - position_in_document})
            split_doc.meta["headlines"] = split_doc_headlines

            # Avoid excessively long documents at all costs. They can be very disruptive for performance.
            # If a document longer than max_chars is found, just hard split it into chunks and log loudly.
            if len(split) <= max_chars:
                split_docs.append(split_doc)
            else:
                logger.error(
                    "Found document with a character count higher than the maximum allowed (%s > %s). "
                    "The document is going to be cut at %s chars, so %s chars are being moved to one (or more) new documents. "
                    "Set the maximum amout of characters allowed through the 'max_chars' parameter. "
                    "Keep in mind that very long Documents can severely impact the performance of Readers.",
                    len(split),
                    max_chars,
                    max_chars,
                    len(split) - max_chars,
                )
                hard_splits = [(split[pos : pos + max_chars], pos) for pos in range(0, len(split), max_chars)]

                split_docs += self._split_document(
                    document=split_doc, splits=hard_splits, max_chars=max_chars, add_page_number=add_page_number
                )

        return split_docs

    def split_by_regex(self, splitter: str, text: str) -> List[str]:
        """
        Split a long text into chunks based on a regex match.

        :param splitter: the text, or regex, to split the text upon
        :param text: the text to split
        :return: the list of splits with the starting position of each.
        """
        matches = [(match.start(), match.end()) for match in re.compile(splitter).finditer(text)]
        if not matches:
            return [text]

        if matches and not matches[-1][1] == len(text):
            matches.append((len(text), len(text)))

        units = []
        for start_match, end_match in zip([(None, 0), *matches[:-1]], matches):
            units.append(text[start_match[1] : end_match[1]])

        return units

    def split_by_tokenizer(self, text: str, tokenizer: TokenizerI) -> List[str]:
        """
        Splits a given text into tokens, preserving all whitespace.

        :param text: the text to tokenize
        :param tokenizer: the tokenizer to use (might be a sentence or word tokenizer)
        :return the tokenized text as a list of strings
        """
        token_spans = tokenizer.span_tokenize(text)

        units = []
        prev_token_start = 0
        for token_start, _ in token_spans:

            if prev_token_start != token_start:
                units.append(text[prev_token_start:token_start])
                prev_token_start = token_start

        if prev_token_start != len(text):
            units.append(text[prev_token_start:])

        return units


def load_tokenizer(
    language_name: Optional[str], tokenizer_model_folder: Optional[Path] = None
) -> nltk.tokenize.punkt.PunktSentenceTokenizer:
    """
    Attempt to load the sentence tokenizer with sensible fallbacks.

    Tried to load from self.tokenizer_model_folder first, then falls back to 'tokenizers/punkt' and eventually
    falls back to the default English tokenizer.
    """
    # Try loading from the specified path
    if tokenizer_model_folder:
        tokenizer_model_path = Path(tokenizer_model_folder) / f"{language_name}.pickle"
        try:
            return nltk.data.load(f"file:{str(tokenizer_model_path)}", format="pickle")
        except LookupError as e:
            logger.exception(f"PreProcessor couldn't load sentence tokenizer from {tokenizer_model_path}")
        except (UnpicklingError, ValueError) as e:
            logger.exception(
                f"PreProcessor couldn't find custom sentence tokenizer model for {language_name} in {tokenizer_model_folder}. "
            )

    # Try loading from the default path
    try:
        return nltk.data.load(f"tokenizers/punkt/{language_name}.pickle")
    except LookupError as e:
        logger.exception(
            "PreProcessor couldn't load sentence tokenizer from the default tokenizer path (tokenizers/punkt/)"
        )
    except (UnpicklingError, ValueError) as e:
        logger.exception(
            "PreProcessor couldn't find custom sentence tokenizer model for %s in the default tokenizer path (tokenizers/punkt/)",
            language_name,
        )

    # Fallback to English from the default path, last shore
    logger.warning(
        "Using an English tokenizer as fallback. You may train your own model and use the 'tokenizer_model_folder' parameter."
    )
    return nltk.data.load(f"tokenizers/punkt/english.pickle")


def longest_common_prefix(texts: list[str], min_len: int, max_len: int) -> Optional[str]:
    """
    Find the longest common prefix across several texts. used for header detection.

    :param texts: list of strings that shall be searched for common prefix
    :param min_len: maximum length to consider
    :param max_len: minimum length to consider
    :return: longest common prefix in all given texts
    """
    if not min_len > 0 or not max_len > 0:
        raise ValueError("Prefix length must be > 0")

    texts = sorted(texts, key=lambda x: len(x))
    for prefix_len in reversed(range(min_len, min(len(texts[0]), max_len) + 1)):
        prefix = texts[0][:prefix_len]
        if all(text[:prefix_len] == prefix for text in texts):
            return prefix
    return None


def longest_common_suffix(texts: list[str], min_len: int, max_len: int) -> Optional[str]:
    """
    Find the longest common suffix across several texts. used for footer detection.

    :param texts: list of strings that shall be searched for common suffix
    :param min_len: maximum length to consider
    :param max_len: minimum length to consider
    :return: longest common suffix in all given texts
    """
    if not min_len > 0 or not max_len > 0:
        raise ValueError("Suffix length must be > 0")

    texts = sorted(texts, key=lambda x: len(x))
    for suffix_len in reversed(range(min_len, min(len(texts[0]), max_len) + 1)):
        suffix = texts[0][len(texts[0]) - suffix_len :]
        if all(text[len(text) - suffix_len :] == suffix for text in texts):
            return suffix
    return None
