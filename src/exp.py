"""Experiment Module
"""
import json
import os
import os.path as osp
import re
from typing import Dict, List, Optional, Union

import pandas as pd
from langchain.chains import RetrievalQAWithSourcesChain
from langchain.chat_models import ChatOpenAI
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.llms import OpenAI
from langchain.output_parsers import PydanticOutputParser
from langchain.prompts import ChatPromptTemplate, PromptTemplate
from langchain.vectorstores import FAISS

from config import LOGGER, MAIN_DIR
from parsers import DrugOutput
from utils import generate_vectorstore


class Experiment:
    """Experiment Module"""

    def __init__(
        self,
        prompt_template: Union[PromptTemplate, ChatPromptTemplate],
        vector_store: str,
        llm_type: str = "gpt-3.5-turbo",
        emb: str = "text-embedding-ada-002",
        keys_json: str = osp.join(MAIN_DIR, "auth", "api_keys.json"),
        temperature: float = 0,
        max_tokens: int = 512,
        gt: Optional[str] = None,
        verbose: bool = False,
    ):
        """Initiate Instance for an experiment run

        Args:
            prompt_template (Union[PromptTemplate, ChatPromptTemplate]): Prompt to be feed to LLM
            vector_store (str): Path to Vector Index Database
            llm_type (str, optional): Type of LLM Model. Defaults to "gpt-3.5-turbo".
            emb (str, optional): Type of Embedding Model. Defaults to "text-embedding-ada-002".
            keys_json (str, optional): Path to API Keys. Defaults to osp.join(MAIN_DIR, "auth", "api_keys.json").
            temperature (float, optional): Temperature Settings for LLM model. Lower temperature makes LLM more deterministic
                while higher temperature makes LLM more random. Defaults to 0.
            max_tokens (int, optional): Max_Tokens Settings for LLM model. Defaults to 512.
            gt (Optional[str], optional): Path to Ground Truth file. Defaults to None.
            verbose (bool, optional): Verbose Setting. Defaults to False.
        """

        self.llm_type = llm_type.lower()
        self.temperature = temperature
        self.max_tokens = max_tokens

        with open(keys_json, "r") as f:
            keys = json.load(f)

        self.openai_key = (
            keys["OPENAI_API_KEY_FOR_GPT4"]
            if self.llm_type == "gpt-4"
            else keys["OPENAI_API_KEY"]
        )

        if isinstance(prompt_template, ChatPromptTemplate):
            self.llm = ChatOpenAI(
                model_name=self.llm_type,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                openai_api_key=self.openai_key,
            )
        else:
            self.llm = OpenAI(
                model_name=self.llm_type,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                openai_api_key=self.openai_key,
            )
        self.embedder = OpenAIEmbeddings(model=emb, openai_api_key=self.openai_key)
        try:
            self.load_vectorstore(vector_store)
        except ValueError:
            print(
                "Vectorstore invalid. Please load valid vectorstore or create new vectorstore."
            )

        self.prompt_template = prompt_template
        self.questions = []
        self.answers = []
        self.sources = []
        self.ground_truth = self.load_groundtruth(gt) if gt else None
        self.drug_parser = PydanticOutputParser(pydantic_object=DrugOutput)
        self.chain = None
        self.verbose = verbose

    def load_vectorstore(self, vectorstore_path: str):
        """Load Vectorstore from path

        Args:
            vectorstore_path (str): Path to vector database folder.
        """
        assert "index.faiss" in os.listdir(
            vectorstore_path
        ) and "index.pkl" in os.listdir(vectorstore_path), "Invalid Vectorstore"
        self.docsearch = FAISS.load_local(vectorstore_path, self.embedder)
        LOGGER.info("Successfully loaded existing vectorstore from local storage")

    def generate_vectorstore(
        self,
        data_directory: Optional[str] = None,
        output_directory: str = "./vectorstore",
        emb_store_type: str = "faiss",
        chunk_size: int = 1000,
        chunk_overlap: int = 250,
        exclude_pages: Optional[Dict] = None,
        pinecone_idx_name: Optional[str] = None,
        additional_docs: Optional[str] = None,
        key_path: Optional[str] = os.path.join(MAIN_DIR, "auth", "api_keys.json"),
    ):
        """Generate New vectorstore

        Args:
            data_directory (str): Directory contains source documents
            output_directory (str, optional): Output directory of vector index database. Defaults to "./vectorstore".
            emb_store_type (str, optional): Type of vector index database. Defaults to "faiss".
            chunk_size (int, optional): Maximum size of text chunks (characters) after split. Defaults to 1000.
            chunk_overlap (int, optional): Maximum overlapping window between text chunks. Defaults to 250.
            exclude_pages (Optional[Dict], optional): Dictionary of pages to be excluded from documents. Defaults to None.
            pinecone_idx_name (Optional[str], optional): Name of pinecone index to be created or loaded. Defaults to None.
            additional_docs (Optional[str], optional): Additional Tables, Images or Json to be added to doc list. Defaults to None.
            key_path (Optional[str], optional): Path to file containing API info.
                Defaults to os.path.join(MAIN_DIR, "auth", "api_keys.json").
        """
        self.docsearch = generate_vectorstore(
            data_directory=data_directory,
            embedder=self.embedder,
            output_directory=output_directory,
            emb_store_type=emb_store_type,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            exclude_pages=exclude_pages,
            pinecone_idx_name=pinecone_idx_name,
            additional_docs=additional_docs,
            key_path=key_path,
        )

    def run_test_cases(
        self, test_cases: Union[List[str], str], only_return_source: bool = False
    ):
        """Run and save test cases to memory

        Args:
            test_cases (Union[List[str], str]): List of test queries.
        """
        if isinstance(test_cases, str):
            with open(test_cases, "r", encoding="utf-8-sig") as f:
                test_cases = f.readlines()
            test_cases = [test_case.rstrip() for test_case in test_cases]

        if not self.chain:
            self._create_retriever_chain()

        if only_return_source:
            LOGGER.info("Perform Semantic Search for Source Documents only (No QA).")

        for test_case in test_cases:
            print("Query: {}".format(test_case))
            sources = []  # All sources for 1 single query
            if only_return_source:
                self.questions.append(test_case)
                self.answers.append(None)
                inputs = {"question": test_case}
                source_documents = self.chain._get_docs(inputs)

            else:
                output = self.chain(test_case)
                self.questions.append(output["question"])
                self.answers.append(output["answer"])
                source_documents = output["source_documents"]

            for document in source_documents:
                sources.append(
                    {
                        "title": document.metadata["title"],
                        "filename": document.metadata["source"].split("/")[-1],
                        "page": document.metadata["page"],
                        "modal": document.metadata["modal"],
                        "text": document.page_content,
                    }
                )

            self.sources.append(sources)

    @staticmethod
    def convert_prompt_to_string(
        prompt: Union[PromptTemplate, ChatPromptTemplate]
    ) -> str:
        """Convert Prompt Object to string format

        Args:
            prompt (Union[PromptTemplate, ChatPromptTemplate]): Prompt Template

        Returns:
            str: Prompt String Template
        """
        return prompt.format(**{v: v for v in prompt.input_variables})

    @staticmethod
    def process_source(source: Dict) -> str:
        """_summary_

        Args:
            source (Dict): Source Document Information

        Returns:
            str: Source Document Information in string
        """
        return "\n\n".join([f"{k}: {v}" for k, v in source.items()])

    def save_json(self, output_path: str):
        """Save Output of test case runs to json file

        Args:
            output_path (str): Output Path to json file.
        """
        output_dict = {}
        output_dict["prompt"] = Experiment.convert_prompt_to_string(
            self.prompt_template
        )
        output_dict["test_cases"] = []

        for question, answer, source in zip(self.questions, self.answers, self.sources):
            output_dict["test_cases"].append(
                {"question": question, "answer": answer, "sources": source}
            )

        with open(output_path, "w") as f:
            json.dump(output_dict, f)

    def load_groundtruth(self, gt_path: str) -> pd.DataFrame:
        """Load Ground Truth information from .csv file

        Args:
            gt_path (str): Path to Ground Truth file

        Returns:
            pd.DataFrame: DataFrame containing Ground Truth data.
        """
        return pd.read_csv(gt_path, encoding="ISO-8859-1")

    def reset(self):
        """Reset queries and answers"""
        self.questions = []
        self.answers = []
        self.sources = []
        self.ground_truth = None

    def load_json(self, json_path: str, reset: bool = False):
        """Load Queries and Answers from Json file

        Args:
            json_path (str): Path to json output file to load into instance
            reset (bool, optional): If reset, clear queries and answers from memory before loading. Defaults to False.
        """
        if reset:
            self.reset()
        with open(json_path, "r") as f:
            input_dict = json.load(f)
        for test_case in input_dict["test_cases"]:
            self.questions.append(test_case["question"])
            self.answers.append(test_case["answer"])
            self.sources.append(test_case["sources"])

    def write_csv(self, output_csv: str):
        """Write questions and answers to .csv files

        Args:
            output_csv (str): Path to output csv file
        """

        pd_answers = [[], []]
        pd_pros = [[], []]
        pd_cons = [[], []]
        pd_sources = [[], [], [], [], [], []]

        for answer, sources in zip(self.answers, self.sources):
            drugs = (
                [
                    self.drug_parser.parse(drug)
                    for drug in re.findall(re.compile(r"{[^{}]+}"), answer)
                ]
                if answer
                else []
            )
            pd_answers[0].append(drugs[0].drug_name if len(drugs) > 0 else None)
            pd_answers[1].append(drugs[1].drug_name if len(drugs) > 1 else None)
            pd_pros[0].append(drugs[0].advantages if len(drugs) > 0 else None)
            pd_cons[0].append(drugs[0].disadvantages if len(drugs) > 0 else None)
            pd_pros[1].append(drugs[1].advantages if len(drugs) > 1 else None)
            pd_cons[1].append(drugs[1].disadvantages if len(drugs) > 1 else None)

            for idx, source in enumerate(sources):
                pd_sources[idx].append(Experiment.process_source(source))

            if idx + 1 < len(pd_sources):
                for i in range(idx + 1, len(pd_sources)):
                    pd_sources[i].append(None)

        info = {"question": self.questions}

        if self.ground_truth is not None:
            info["gt_rec1"] = self.ground_truth["Recommendation 1"].tolist()
            info["gt_rec2"] = self.ground_truth["Recommendation 2"].tolist()
            info["gt_rec3"] = self.ground_truth["Recommendation 3"].tolist()
            info["gt_avoid"] = self.ground_truth["Drug Avoid"].tolist()
            info["gt_reason"] = self.ground_truth["Reasoning"].tolist()

        info["prompt"] = [
            Experiment.convert_prompt_to_string(self.prompt_template)
        ] * len(self.questions)
        info["raw_answer"] = self.answers
        info["answer1"] = pd_answers[0]
        info["pro1"] = pd_pros[0]
        info["cons1"] = pd_cons[0]
        info["answer2"] = pd_answers[1]
        info["pro2"] = pd_pros[1]
        info["cons2"] = pd_cons[1]
        info["source1"] = pd_sources[0]
        info["source2"] = pd_sources[1]
        info["source3"] = pd_sources[2]
        info["source4"] = pd_sources[3]
        info["source5"] = pd_sources[4]
        info["source6"] = pd_sources[5]

        panda_df = pd.DataFrame(info)

        panda_df.to_csv(output_csv, header=True)

    def _create_retriever_chain(
        self,
        chain_type: str = "stuff",
        return_source_documents: bool = True,
        reduce_k_below_max_tokens: bool = True,
    ):
        """Initiate QA from Source Chain

        Args:
            chain_type (str, optional): Chain Type. Can be stuff|map_reduce|refine|map_rerank. Defaults to "stuff".
            return_source_documents (bool, optional): Whether to return source documents along side answers. Defaults to True.
            reduce_k_below_max_tokens (bool, optional): _description_. Defaults to True.
        """
        self.chain = RetrievalQAWithSourcesChain.from_chain_type(
            llm=self.llm,
            chain_type=chain_type,
            retriever=self.docsearch.as_retriever(),
            return_source_documents=return_source_documents,
            chain_type_kwargs={"prompt": self.prompt_template},
            reduce_k_below_max_tokens=reduce_k_below_max_tokens,
            verbose=self.verbose,
        )
