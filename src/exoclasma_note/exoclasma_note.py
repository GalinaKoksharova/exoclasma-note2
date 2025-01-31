__version__ = "0.9.0"

from contextlib import contextmanager
from copy import deepcopy as dc
from glob import glob
from multiprocessing import cpu_count, Pool
from pandarallel import pandarallel #
from typing import Union
import argparse
import bz2
import base64
import datetime
import functools
import glob
import gzip
import io
import json
import logging
import math
import numpy #
import os
import pandas #
import re
import subprocess
import sys
import tabix #
import tempfile
import time
import warnings

## ------======| LOGGING |======------

def DefaultLogger(
		LogFileName: str,
		Level: int = logging.DEBUG) -> logging.Logger:
	
	# Format
	Formatter = "%(asctime)-30s%(levelname)-13s%(funcName)-25s%(message)s"
	
	# Compose logger
	Logger = logging.getLogger("default_logger")
	logging.basicConfig(level=Level, format=Formatter)
	
	# Add log file
	Logger.handlers = []
	LogFile = logging.FileHandler(LogFileName)
	LogFile.setLevel(Level)
	LogFile.setFormatter(logging.Formatter(Formatter))
	Logger.addHandler(LogFile)
	
	# Return
	return Logger

## ------======| I/O |======------

def SaveJSON(Data: list, FileName: str) -> None: json.dump(Data, open(FileName, 'w'), indent=4, ensure_ascii=False)

def GzipCheck(FileName: str) -> bool: return open(FileName, 'rb').read(2).hex() == "1f8b"

def Bzip2Check(FileName: str) -> bool: return open(FileName, 'rb').read(3).hex() == "425a68"

def OpenAnyway(FileName: str,
		Mode: str,
		Logger: logging.Logger):
	
	try:
		IsGZ = GzipCheck(FileName=FileName)
		IsBZ2 = Bzip2Check(FileName=FileName)
		return gzip.open(FileName, Mode) if IsGZ else (bz2.open(FileName, Mode) if IsBZ2 else open(FileName, Mode))
	except OSError as Err:
		ErrorMessage = f"Can't open the file '{FileName}' ({Err})"
		Logger.error(ErrorMessage)
		raise OSError(ErrorMessage)

def GenerateFileNames(
		Unit: dict,
		Options: dict) -> dict:
	
	Unit['OutputDir'] = os.path.join(Options["PoolDir"], Unit['ID'])
	IRs = os.path.join(Unit['OutputDir'], "IRs")
	FileNames = {
		"OutputDir": Unit['OutputDir'],
		"IRs": IRs,
		"Log": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.pipeline_log.txt"),
		"PrimaryBAM": os.path.join(IRs, f"{Unit['ID']}.primary.bam"),
		"PrimaryStats": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.primary_stats.txt"),
		"DuplessBAM": os.path.join(IRs, f"{Unit['ID']}.dupless.bam"),
		"DuplessMetrics": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.md_metrics.txt"),
		"RecalBAM": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.final.bam"),
		"CoverageStats": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.coverage.txt"),
		"VCF": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.unfiltered.vcf"),
		"AnnovarTable": os.path.join(IRs, f"{Unit['ID']}.annovar.tsv"),
		"Gff3Table": os.path.join(IRs, f"{Unit['ID']}.curebase.tsv"),
		"FilteredXLSX": os.path.join(Unit['OutputDir'], f"{Unit['ID']}.AnnoFit.xlsx")
	}
	return FileNames

## ------======| THREADING |======------

@contextmanager
def Threading(Name: str,
		Logger: logging.Logger,
		Threads: int) -> None:
	
	# Timestamp
	StartTime = time.time()
	
	# Pooling
	pool = Pool(Threads)
	yield pool
	pool.close()
	pool.join()
	del pool
	
	# Timestamp
	Logger.info(f"{Name} finished on {str(Threads)} threads, summary time - %s" % (SecToTime(time.time() - StartTime)))

## ------======| SUBPROCESS |======------

def SimpleSubprocess(
		Name: str,
		Command: str,
		CheckPipefail: bool = False,
		Env: Union[str, None] = None,
		AllowedCodes: list = []) -> None:
	
	# Timestamp
	StartTime = time.time()
	
	# Compose command
	Command = (f"source {Env}; " if Env is not None else f"") + (f"set -o pipefail; " if CheckPipefail else f"") + Command
	logging.debug(Command)
	
	# Shell
	Shell = subprocess.Popen(Command, shell=True, executable="/bin/bash", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	Stdout, Stderr = Shell.communicate()
	if Shell.returncode != 0 and Shell.returncode not in AllowedCodes:
		ErrorMessages = [
			f"Command '{Name}' has returned non-zero exit code [{str(Shell.returncode)}]",
			f"Command: {Command}",
			f"Details: {Stderr.decode('utf-8')}"
			]
		for line in ErrorMessages: logging.error(line)
		raise OSError(f"{ErrorMessages[0]}\n{ErrorMessages[2]}")
	if Shell.returncode in AllowedCodes: logging.warning(f"Command '{Name}' has returned ALLOWED non-zero exit code [{str(Shell.returncode)}]")
	
	# Timestamp
	logging.info(f"{Name} - %s" % (SecToTime(time.time() - StartTime)))
	
	# Return
	return Stdout[:-1]

## ------======| MISC |======------

def SecToTime(Sec: float) -> str: return str(datetime.timedelta(seconds=int(Sec)))

def MultipleTags(Tag: str, List: list, Quoted: bool = True) -> str: return ' '.join([(f"{Tag} \"{str(item)}\"" if Quoted else f"{Tag} {str(item)}") for item in List])

def PrepareGenomeBED(
		Reference: str,
		GenomeBED: str,
		Logger: logging.Logger) -> None:
	
	MODULE_NAME = "PrepareGenomeBED"
	
	# Processing
	SimpleSubprocess(
		Name = f"{MODULE_NAME}.Create",
		Command = "awk 'BEGIN {FS=\"\\t\"}; {print $1 FS \"0\" FS $2}' \"" + Reference + ".fai\" > \"" + GenomeBED + "\"",
		Logger = Logger)


logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.DEBUG)

# ------======| ANNOVAR |======------

def ANNOVAR(
		InputVCF: str,
		OutputTSV: str,
		DBFolder: str,
		AnnovarFolder: str,
		GenomeAssembly: str,
		Databases: list = [],
		GFF3List: list = [],
		Threads: int = cpu_count()) -> None:
	
	MODULE_NAME = "ANNOVAR"
	
	assert bool(Databases) != bool(GFF3List), f"Either Databases or GFF3 files must be defined"
	
	# Logging
	for line in [f"Input VCF: {InputVCF}", f"Output TSV: {OutputTSV}", f"Genome Assembly: {GenomeAssembly}", f"Databases Dir: {DBFolder}"] + ([] if not Databases else [f"Databases: {'; '.join([(item['Protocol'] + '[' + item['Operation'] + ']') for item in Databases])}"]) + ([] if not GFF3List else [f"Databases: GFF3, {len(GFF3List)} items [r]"]): logging.info(line)
	
	with tempfile.TemporaryDirectory() as TempDir:
		
		# Options
		TableAnnovarPath = os.path.join(AnnovarFolder, "table_annovar.pl")
		Protocol = ','.join([item["Protocol"] for item in Databases] + ["gff3" for item in GFF3List])
		Operation = ','.join([item["Operation"] for item in Databases] + ["r" for item in GFF3List])
		GFFs = "--gff3dbfile " + ','.join(GFF3List)
		TempVCF = os.path.join(TempDir, "temp.vcf")
		AnnotatedTXT = f"{TempVCF}.{GenomeAssembly}_multianno.txt"
		
		# Processing
		SimpleSubprocess(
			Name = f"{MODULE_NAME}.TempVCF",
			Command = f"zcat \"{InputVCF}\" > \"{TempVCF}\"")
		SimpleSubprocess(
			Name = f"{MODULE_NAME}.Annotation",
			Command = f"perl \"{TableAnnovarPath}\" \"{TempVCF}\" \"{DBFolder}\" --buildver {GenomeAssembly} --protocol {Protocol} --operation {Operation} {GFFs} --remove --vcfinput --thread {Threads}",
			AllowedCodes = [25])
		SimpleSubprocess(
			Name = f"{MODULE_NAME}.CopyTSV",
			Command = f"cp \"{AnnotatedTXT}\" \"{OutputTSV}\"")

# ------======| CUSTOM REGION-BASED ANNOTATIONS |======------

def Tsv2Gff3(
		dbName: str,
		InputTSV: str,
		ChromCol: str,
		StartCol: str,
		EndCol: str,
		OutputGFF3: str,
		Reference: str,
		Threads: int) -> None:
	
	MODULE_NAME = "Tsv2Gff3"
	
	pandarallel.initialize(nb_workers=Threads, verbose=1)
	
	# Logging
	for line in [f"Name: {dbName}", f"Input TSV db: {InputTSV}", f"Output GFF3: {OutputGFF3}"]: logging.info(line)
	
	# Options
	AnchorCols = [ChromCol, StartCol, EndCol]
	DataOrder = [ChromCol, "sample", "type", StartCol, EndCol, "score", "strand", "phase", "attributes"]
	
	# Prepare faidx
	Faidx = pandas.read_csv(Reference + ".fai", sep='\t', header=None).assign(Tag="##sequence-region", Start=1)[["Tag", 0, "Start", 1]]
	Chroms = {value: index for index, value in enumerate(Faidx[0].to_list())}
	
	# Load data
	Data = pandas.read_csv(InputTSV, sep='\t')
	AttributeCols = [item for item in Data.columns.to_list() if item not in AnchorCols]
	
	# Filter & sort intervals by reference
	Filtered = [item for item in list(set(Data[ChromCol].to_list())) if item not in Chroms.keys()]
	if Filtered: logging.warning(f"Contigs will be removed from database \"{dbName}\": {', '.join(sorted(Filtered))}")
	Data = Data[Data[ChromCol].parallel_apply(lambda x: x in Chroms.keys())]
	Data["Rank"] = Data[ChromCol].map(Chroms)
	Data.sort_values(["Rank", StartCol], inplace=True)
	if Data.shape[0] == 0:
		ErrorMessage = f"Database \"{dbName}\" and reference \"{Reference}\" have no matching columns"
		logging.error(ErrorMessage)
		raise RuntimeError(ErrorMessage)
	
	# Processing
	Attributes = Data[AttributeCols].parallel_apply(lambda x: "ID=" + base64.b16encode(json.dumps(x.to_dict()).encode('utf-8')).decode('utf-8'), axis=1)
	Data.drop(columns=AttributeCols, inplace=True)
	Data[StartCol] = Data[StartCol].parallel_apply(lambda x: 1 if x == 0 else x)
	Data = Data.assign(sample=dbName, type="region", attributes=Attributes,	score=".", strand=".", phase=".")[DataOrder]
	
	# Save
	with open(OutputGFF3, 'wt') as O:
		O.write(
			"##gff-version 3\n" +
			Faidx.to_csv(sep=' ', index=False, header=False) + 
			Data.to_csv(sep='\t', index=False, header=False))
	
	# Return expected cols
	return [f"{str(dbName)}.{str(item)}" for item in AttributeCols] if AttributeCols else [ str(dbName) ]

def CureBase(
		InputVCF: str,
		OutputTSV: str,
		Databases: list,
		AnnovarFolder: str,
		GenomeAssembly: str,
		Reference: str,
		DBDir: str,
		Threads: int) -> None:
	
	MODULE_NAME = "CureBase"
	
	# Initialize Pandarallel
	pandarallel.initialize(nb_workers=Threads, verbose=1)
	
	with tempfile.TemporaryDirectory() as TempDir:
		
		Gff3List, ExpectedCols = [], []
		
		for index, DB in enumerate(Databases):
			Gff3File = os.path.join(TempDir, f"database_{str(index)}.gff3")
			ExpectedCols += Tsv2Gff3(
				dbName = DB["Name"],
				InputTSV = os.path.join(DBDir, DB["FileName"]),
				ChromCol = DB["ChromColumn"],
				StartCol = DB["StartColumn"],
				EndCol = DB["EndColumn"],
				OutputGFF3 = Gff3File,
				Reference = Reference,
				Threads = Threads)
			Gff3List += [ f"database_{str(index)}.gff3" ]
		
		TempTSV = os.path.join(TempDir, f"temp.tsv")
		ANNOVAR(
			InputVCF = InputVCF,
			OutputTSV = TempTSV,
			GFF3List = Gff3List,
			DBFolder = TempDir,
			AnnovarFolder = AnnovarFolder,
			GenomeAssembly = GenomeAssembly,
			Threads = Threads)
		
		SNPdata = ['Chr', 'Start', 'End', 'Ref', 'Alt']
		Data = pandas.read_csv(TempTSV, sep='\t', dtype=str)
		NewColumns = {f"gff3{'' if index == 0 else str(index + 1)}": item["Name"] for index, item in enumerate(Databases)}
		Data = Data[SNPdata + list(NewColumns.keys())]
		Data[list(NewColumns.keys())] = Data[list(NewColumns.keys())].parallel_applymap(lambda x: '.' if x == '.' else [json.loads(base64.b16decode(item.encode('utf-8')).decode('utf-8')) for item in x.split("=")[1].split(",")])
		for Col in list(NewColumns.keys()):
			NewCols = Data[Col][Data[Col] != '.']
			if NewCols.size > 0:
				NewCols = NewCols.parallel_apply(lambda LD: pandas.Series({str(NewColumns[Col]): ["yes"]} if not LD[0] else {f"{str(NewColumns[Col])}.{str(k)}": [dic[k] for dic in LD] for k in LD[0]}))
				Data = pandas.concat([Data, NewCols], axis=1)
			else: logging.warning(f"Database has no intersections with variants: {str(NewColumns[Col])}")
		Data = Data.drop(columns=list(NewColumns.keys()))
		MissingCols = [item for item in ExpectedCols if item not in Data.columns.to_list()]
		Data[MissingCols] = float("nan")
		InformationCols = [item for item in Data.columns.to_list() if item not in SNPdata]
		Data[InformationCols] = Data[InformationCols].parallel_applymap(lambda x: '.' if x != x else '; '.join([str(item) for item in sorted(list(set(x)))]))
		Data = Data[SNPdata + ExpectedCols]
		
		# Merge Annovar & Gff3
		AnnovarTable = pandas.read_csv(OutputTSV, sep='\t', dtype=str)
		Data = pandas.merge(AnnovarTable, Data, how='left', on=['Chr', 'Start', 'End', 'Ref', 'Alt']).sort_values(by=['Chr', 'Start'])
		Data.to_csv(OutputTSV, sep='\t', index=False)

# ------======| ANNOFIT |======------

def AnnoFit(
		InputTSV: str,
		OutputXLSX: str,
		HGMD: str,
		AnnovarFolder: str,
		AnnoFitConfig: str,
		ChunkSize: int,
		Filtering: str = "full",
		Threads: int = cpu_count()) -> None:
	
	MODULE_NAME = "AnnoFit"
	
	# Logging
	for line in [f"Input TSV: {InputTSV}", f"Output XLSX: {OutputXLSX}", f"Chunk Size: {str(ChunkSize)}"]: logging.info(line)
	
	# Initialize Pandarallel
	pandarallel.initialize(nb_workers=Threads, verbose=1)
	
	# Global func
	def FormatInt(String: str) -> Union[int, None]:
		try:
			return int(String)
		except ValueError:
			return None
	def FormatFloat(String: str) -> Union[float, None]:
		try:
			return float(String)
		except ValueError:
			return None
	def SqueezeTable(DataFrame: pandas.DataFrame) -> pandas.Series:
		if DataFrame.shape[0] == 1: return DataFrame.iloc[0]
		if DataFrame.shape[0] == 0: return pandas.Series(index=DataFrame.columns.to_list(), dtype=object).fillna('.')
		Squeezed = pandas.Series(dtype=object)
		for col in DataFrame.columns.to_list():
			Squeezed[col] = ';'.join(sorted([str(x) for x in DataFrame[col].to_list() if x != '.']))
			if Squeezed[col] == '': Squeezed[col] = '.'
		return Squeezed
	
	# Format func
	def FormatCoordinates(String: str) -> Union[int, str]:
		Result = FormatInt(String)
		return ('.' if Result is None else Result)
	def FormatPopulationFreq(String: str) -> float:
		Result = FormatFloat(String)
		return (-1.0 if Result is None else Result)
	def FormatVcfMetadata(Block: pandas.Series) -> dict:
		Block = {"Header": Block["VCF.FORMAT"], "Data": Block["VCF.SAMPLE"], "Name": Block.name}
		Block["Header"] = [f"VCF.{item}" for item in str(Block["Header"]).split(":")]
		Block["Data"] = str(Block["Data"]).split(":")
		Result = {"Name": Block["Name"]}
		if len(Block["Header"]) != len(Block["Data"]): return Result
		for Num in range(len(Block["Header"])): Result[Block["Header"][Num]] = Block["Data"][Num]
		return Result
	def FormatGenesOrFunction(Series: pandas.Series) -> str:
		Genes = [item for item in Series.to_list() if ((type(item) is str) and (item != "."))]
		Genes = [item.split(';') for item in Genes]
		Genes = list(set([item for sublist in Genes for item in sublist]))
		return (';'.join(sorted(Genes)) if Genes else '.')
	def FormatRevel(Value: str) -> str:
		Value = FormatFloat(Value)
		return ("U" if Value is None else ("D" if (Value <= 0.5) else "T"))
	def FormatDbscSNV(Series: pandas.Series) -> str:
		Result = [FormatFloat(item) for item in Series.to_list()]
		return ("." if any([item is None for item in Result]) else ("D" if any([item > 0.6 for item in Result]) else "T"))
	def FormatMutPred(Str: str) -> str:
		Result = FormatFloat(Str)
		return ("U" if Result is None else ("D" if (Result >= 0.9) else "T"))
	def FormatOmimCodes(Series: pandas.Series) -> dict:
		Groups = re.findall("\\[MIM:([\\d]+)\\]", str(Series["Disease_description"]))
		Result = {"Name": Series.name}
		for num, item in enumerate(Groups): Result[f"OMIM-{num:02d}"] = f"=HYPERLINK(\"https://omim.org/entry/{item}\", \"{item}\")"
		return Result
	def FormatGenotype(Str: str) -> str:
		Lst = [FormatInt(item) for item in Str.split('/')]
		if (len(Lst) != 2) or any([item is None for item in Lst]): return '.'
		return ('HOMO' if ((Lst[0] == Lst[1]) and (Lst[0] != 0)) else Str)
	def FormatConservation(Series: pandas.Series) -> str:
		Series = Series.to_list()
		for i in range(len(Series)):
			Rank = FormatFloat(Series[i])
			Series[i] = "U" if Rank is None else ("D" if Rank >= 0.7 else "T")
		return "/".join([str(Series.count("D")), str(Series.count("D") + Series.count("T"))]) if Series.count("U") < len(Series) else '.'
	def FormatDetails(Series: pandas.Series) -> str:
		lst = [item for item in Series.to_list() if ((type(item) is str) and (item != "."))]
		return '.' if not lst else ';'.join(lst)
	def FormatExonPrediction(Series: pandas.Series) -> str:
		Result = ''.join(Series.to_list())
		return ('.' if (Result.count('D') + Result.count('T') == 0) else '/'.join([str(Result.count('D')), str(Result.count('D') + Result.count('T'))]))
	def	FormatGIAB(Series: pandas.Series) -> str:
		Result = sorted(list(set([str(key[5:]) for key, value in Series.to_dict().items() if value != '.'])))
		return '.' if not Result else ';'.join(Result)
	def	FormatNCBIProblems(Series: pandas.Series) -> str:
		Result = sorted(list(set([str(key[5:-6]) for key, value in Series.to_dict().items() if key[-6:] == '.start' and value != '.'])), key=lambda x: Config["NCBI_Problems_Ranks"][x])
		return '.' if not Result else Result[-1]
	FormatdbSNP = lambda x: '.' if x == '.' else f"=HYPERLINK(\"https://www.ncbi.nlm.nih.gov/snp/{x}\", \"{x}\")"
	FormatUCSC = lambda x: f"=HYPERLINK(\"https://genome.ucsc.edu/cgi-bin/hgTracks?db=hg19&position={x['Chr']}%3A{str(x['Start'])}%2D{str(x['End'])}\", \"{x['Chr']}:{str(x['Start'])}\")"
	FormatGenomeBrowser = lambda x: '.' if x == '.' else f"=HYPERLINK(\"https://www.genecards.org/Search/Keyword?queryString={'%20OR%20'.join(['%5Baliases%5D(%20' + str(item) + '%20)' for item in x.split(';')])}&keywords={','.join([str(item) for item in x.split(';')])}\", \"{x}\")"
	
	# Filter func
	def FilterpLi(Str: str) -> bool:
		Result = [FormatFloat(item) for item in Str.split(';')]
		return (False if any([item is None for item in Result]) else any([item >= 0.9 for item in Result]))
	def FilterDepth(Str: str) -> bool:
		Result = [FormatInt(item) for item in Str.split(',')]
		return (False if any([item is None for item in Result]) else any([item >= 4 for item in Result]))
	def FilterExonPrediction(Str: str) -> bool:
		Result = FormatInt(Str.split('/')[0])
		return (False if Result is None else (Result >= 3))
	FilterOmimDominance = lambda x: len(re.findall('[\W]dominant[\W]', str(x).lower())) != 0
	FilterNoInfo = lambda x: (x["pLi"] == '.') and (x["Disease_description"] == '.') or (len(re.findall('([\W]dominant[\W])|([\W]recessive[\W])', str(["Disease_description"]).lower())) == 0)
	
	# Load Data
	GlobalTime = time.time()
	StartTime = time.time()
	Result = None
	Config = AnnoFitConfig
	HGMDTable = pandas.read_csv(HGMD, sep='\t', dtype=str)
	for Col in ['Chromosome/scaffold position start (bp)', 'Chromosome/scaffold position end (bp)']: HGMDTable[Col] = HGMDTable[Col].parallel_apply(FormatCoordinates)
	XRefTable = pandas.read_csv(os.path.join(AnnovarFolder, "example/gene_fullxref.txt"), sep='\t', dtype=str).set_index("#Gene_name").rename_axis(None, axis=1)
	logging.info(f"Data loaded - %s" % (SecToTime(time.time() - StartTime)))
	
	for ChunkNum, Data in enumerate(pandas.read_csv(InputTSV, sep='\t', dtype=str, chunksize=ChunkSize)):
		
		# ANNOVAR Table
		ChunkTime = time.time()
		StartTime = time.time()
		
		Data.fillna('.', inplace=True)
		
		# Problematic Regions
		Data["GIAB_Problems"] = Data[Config["GIAB"]].parallel_apply(FormatGIAB, axis=1)
		Data["NCBI_Problems"] = Data[Config["NCBI_Problems"]].parallel_apply(FormatNCBIProblems, axis=1)
		Data = Data.rename(columns={"ENCODE_Blacklist.name": "ENCODE_Blacklist", 'UCSC_UnusualRegions.name': 'UCSC_UnusualRegions'})
		
		# Basic info format
		Data.rename(columns=Config["OtherInfo"], inplace=True) # Rename OtherInfo
		Data[["Start", "End"]] = Data[["Start", "End"]].parallel_applymap(FormatCoordinates) # Prepare coords
		for Col in Config["WipeIntergene"]: Data[Config["WipeIntergene"][Col]] = Data[[Col, Config["WipeIntergene"][Col]]].parallel_apply(lambda x: "." if any([item in Config["IntergeneSynonims"] for item in str(x[Col]).split(';')]) else x[Config["WipeIntergene"][Col]], axis=1)
		Data["AnnoFit.GeneName"] = Data[Config["GeneNames"]].parallel_apply(FormatGenesOrFunction, axis=1) # Gene Names
		Data["AnnoFit.Func"] = Data[Config["Func"]].parallel_apply(FormatGenesOrFunction, axis=1) # Gene Func
		Data["AnnoFit.ExonicFunc"] = Data[Config["ExonicFunc"]].parallel_apply(FormatGenesOrFunction, axis=1) # Gene Exonic Func
		Data["AnnoFit.Details"] = Data[Config["Details"]].parallel_apply(FormatDetails, axis=1) # Details
		
		# VCF Data
		VCF_Metadata = pandas.DataFrame(Data[["VCF.FORMAT", "VCF.SAMPLE"]].parallel_apply(FormatVcfMetadata, axis=1).to_list()).set_index("Name").rename_axis(None, axis=1)
		Data.drop(columns=["VCF.FORMAT", "VCF.SAMPLE"], inplace=True)
		Data = pandas.concat([Data, VCF_Metadata], axis=1, sort=False)
		Data.fillna('.', inplace=True)
		del VCF_Metadata
		Data["VCF.GT"] = Data["VCF.GT"].parallel_apply(FormatGenotype) # Prepare Genotype
		
		# Symbol Predictions
		for Column in Config["SymbolPred"]: Data[Column["Name"]] = Data[Column["Name"]].parallel_apply(lambda x: "U" if x not in Column["Symbols"] else Column["Symbols"][x])
		Data["REVEL"] = Data["REVEL"].parallel_apply(FormatRevel)
		Data["MutPred_rankscore"] = Data["MutPred_rankscore"].parallel_apply(FormatMutPred)
		ColNames = [item["Name"] for item in Config["SymbolPred"]] + ["REVEL", "MutPred_rankscore"]
		Data["AnnoFit.ExonPred"] = Data[ColNames].parallel_apply(FormatExonPrediction, axis=1)
		Data["AnnoFit.SplicePred"] = Data[Config["dbscSNV"]].parallel_apply(FormatDbscSNV, axis=1) # dbscSNV
		Data["AnnoFit.Conservation"] = Data[Config["ConservationRS"]].parallel_apply(FormatConservation, axis=1) # Conservation
		
		# Population
		for Col in Config["MedicalPopulationData"]: Data[Col] = Data[Col].parallel_apply(FormatPopulationFreq)
		Data["AnnoFit.PopFreqMax"] = Data[Config["PopulationData"]].parallel_apply(lambda x: x.apply(FormatPopulationFreq).max(), axis=1)
		
		# Shorten table
		Data = Data[Config["ShortVariant"]]
		logging.info(f"ANNOVAR table is prepared - %s" % (SecToTime(time.time() - StartTime)))
		
		# Merge with HGMD
		StartTime = time.time()
		Data = pandas.merge(Data, HGMDTable, how='left', left_on=["Chr", "Start", "End"], right_on=["Chromosome/scaffold name", "Chromosome/scaffold position start (bp)", "Chromosome/scaffold position end (bp)"])
		Data.rename(columns={"Variant name": "HGMD"}, inplace=True)
		Data.fillna('.', inplace=True)
		logging.info(f"HGMD merged - %s" % (SecToTime(time.time() - StartTime)))
		
		# Merge with XRef
		StartTime = time.time()
		XRef = Data["AnnoFit.GeneName"].parallel_apply(lambda x: SqueezeTable(XRefTable.loc[[item for item in x.split(';') if item in XRefTable.index],:]))
		Data = pandas.concat([Data, XRef], axis=1, sort=False)
		Data.fillna('.', inplace=True)
		del XRef
		logging.info(f"XRef merged - %s" % (SecToTime(time.time() - StartTime)))
		
		if Filtering == "full":
			# Base Filtering
			StartTime = time.time()
			Filters = {}
			Filters["DP"] = Data["VCF.AD"].parallel_apply(FilterDepth)
			Filters["OMIM"] = Data["Disease_description"].parallel_apply(lambda x: x != '.')
			Filters["HGMD"] = Data['HGMD'].parallel_apply(lambda x: x != '.')
			Filters["PopMax"] = Data["AnnoFit.PopFreqMax"] < Config["PopMax_filter"]
			Filters["ExonPred"] = Data["AnnoFit.ExonPred"].parallel_apply(FilterExonPrediction)
			Filters["SplicePred"] = Data["AnnoFit.SplicePred"].parallel_apply(lambda x: x in Config["SplicePred_filter"])
			Filters["IntronPred"] = Data["regsnp_disease"].parallel_apply(lambda x: x in Config["IntronPred_filter"])
			Filters["Significance"] = Data["InterVar_automated"].parallel_apply(lambda x: x in Config["InterVar_filter"])
			Filters["CLINVAR"] = Data["CLNSIG"].parallel_apply(lambda x: any([item in Config["CLINVAR_filter"] for item in str(x).split(',')]))
			Filters["ExonicFunc"] = Data["AnnoFit.ExonicFunc"].parallel_apply(lambda x: any([item in Config["ExonicFunc_filter"] for item in str(x).split(';')]))
			Filters["ncRNA"] = Data["AnnoFit.Func"].parallel_apply(lambda x: any([item in Config["ncRNA_filter"] for item in str(x).split(';')]))
			Filters["Splicing"] = Data["AnnoFit.Func"].parallel_apply(lambda x: any([item in Config["Splicing_filter"] for item in str(x).split(';')]))
			Filters["Problematic"] = Data[list(Config["Problems"].keys())].parallel_apply(lambda x: all([(x[index] not in item) for index, item in Config["Problems"].items()]), axis=1)
			
			Data = Data[Filters["DP"] & Filters["PopMax"] & Filters["Problematic"] & ( Filters["HGMD"] | Filters["ExonPred"] | Filters["SplicePred"] | Filters["IntronPred"] | Filters["Significance"] | Filters["CLINVAR"] | Filters["ExonicFunc"] | Filters["Splicing"] | (Filters["ncRNA"] & Filters["OMIM"]))]
		logging.info(f"Base filtering is ready - %s" % (SecToTime(time.time() - StartTime)))
		
		#Concat chunks
		Result = Data if Result is None else pandas.concat([Result, Data], axis=0, ignore_index=True)
		logging.info(f"Chunk #{str(ChunkNum + 1)} - %s" % (SecToTime(time.time() - ChunkTime)))
	
	# Compound
	if Filtering == "full":
		Result['Annofit.Compound'] = Result['AnnoFit.GeneName'].parallel_apply(lambda x: ';'.join([str(Result['AnnoFit.GeneName'].apply(lambda y: gene in y.split(';')).value_counts()[True]) for gene in x.split(';')]))
	else: Result['Annofit.Compound'] = "."
	
	if Filtering == "full":
		# Dominance Filtering
		StartTime = time.time()
		Filters = {}
		Filters["pLi"] = Result["pLi"].parallel_apply(FilterpLi)
		Filters["OMIM_Dominance"] = Result["Disease_description"].parallel_apply(FilterOmimDominance)
		Filters["Zygocity"] = Result["VCF.GT"].parallel_apply(lambda x: x == 'HOMO')
		Filters["NoInfo"] = Result[["pLi", "Disease_description"]].parallel_apply(FilterNoInfo, axis=1)
		Filters["Compound_filter"] = Result['Annofit.Compound'].parallel_apply(lambda x: any([int(item) > 1 for item in x.split(';')]))
		Result = Result[ Filters["Compound_filter"] | Filters["pLi"] | Filters["OMIM_Dominance"] | Filters["Zygocity"] | Filters["NoInfo"] ]
		logging.info(f"Filtering is ready - %s" % (SecToTime(time.time() - StartTime)))
		
	Result = Result.sort_values(by=["Chr", "Start", "End"])
	Result["UCSC"] = "."
	
	if Filtering == "full":
		# Make genes list
		StartTime = time.time()
		Genes = [item.split(';') for item in Result["AnnoFit.GeneName"].to_list()]
		Genes = list(set([item for sublist in Genes for item in sublist]))
		GenesTable = XRefTable.loc[[item for item in Genes if item in XRefTable.index],:].reset_index().rename(columns={"index": "#Gene_name"}).sort_values(by=["#Gene_name"])[Config["ShortGenesTable"]]
		# TODO NoInfo Genes?
		del XRefTable
		logging.info(f"Genes list is ready - %s" % (SecToTime(time.time() - StartTime)))
		
		# Hyperlinks
		StartTime = time.time()
		Result["avsnp150"] = Result["avsnp150"].parallel_apply(FormatdbSNP)
		Result["UCSC"] = Result[["Chr", "Start", "End"]].parallel_apply(FormatUCSC, axis=1)
		Result["AnnoFit.GeneName"] = Result["AnnoFit.GeneName"].apply(FormatGenomeBrowser)
		OMIM_links = pandas.DataFrame(GenesTable[["pLi", "Disease_description"]].parallel_apply(FormatOmimCodes, axis=1).to_list()).set_index("Name").fillna('.').rename_axis(None, axis=1)
		GenesTable = pandas.concat([GenesTable, OMIM_links], axis=1, sort=False)
		logging.info(f"Hyperlinks are ready - %s" % (SecToTime(time.time() - StartTime)))
	
    #find entertainment variants rs
	if Filtering == "no":
		rs_to_find = pandas.read_excel("/storage2/gskoksharova/exoclasma/pipe/ТЗ rs.xlsx")
		rs_found = pandas.merge(Result["avsnp150"], rs_to_find, how="inner")
    
	# Save result
	StartTime = time.time()
	Result = Result[Config["FinalVariant"]]
	Result.insert(0, 'Comment', '')
	if Filtering == "full":
		with pandas.ExcelWriter(OutputXLSX) as Writer:
			Result.to_excel(Writer, "Variants", index=False)
			GenesTable.to_excel(Writer, "Genes", index=False)
			Writer.save()
	else:
		with pandas.ExcelWriter(OutputXLSX) as Writer:
			Result.to_excel(Writer, "Variants", index=False)
			rs_found.to_excel(Writer, "Ent variants found", index=False)
			Writer.save()
	
	logging.info(f"Files saved - %s" % (SecToTime(time.time() - StartTime)))
	logging.info(f"{MODULE_NAME} finish - %s" % (SecToTime(time.time() - GlobalTime)))

# ------======| ANNOTATION PIPELINE |======------

def AnnoPipe(
		AnnovarFolder: str,
		UnitsFile: str,
		Filtering: str, Genome) -> None:
	DaemonicConf = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'DaemonicPipeline_config.json'), 'rt'))
	AnnofitConf = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'AnnoFit_config.json'), 'rt'))
	
	Unit = json.load(open(UnitsFile, 'rt'))
	Unit['AnnovarFolder'] = os.path.realpath(AnnovarFolder)
	Unit['AnnovarXRef'] = os.path.join(Unit['AnnovarFolder'], DaemonicConf['AnnovarXRefPath'])
	Unit['AnnovarDatabasesPath'] = os.path.join(Unit['AnnovarFolder'], DaemonicConf['AnnovarDBFolder'])
	Unit['AnnovarDatabases'] = DaemonicConf['AnnovarDatabases']
	Unit['Reference']['GenomeInfo']['annovar_alias'] = Genome
	Unit['GFF3'] = DaemonicConf['GFF3']
	Unit['HGMDPath'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), DaemonicConf['HGMDPath'])
	Unit['AnnoFit'] = AnnofitConf
	Unit['Output']['AnnovarTable'] = f'_temp.{Unit["ID"]}.annovar.tsv'
	Unit['Output']['VariantsTable'] = {'full': f'{Unit["ID"]}.variants.xlsx', 'no': f'{Unit["ID"]}.variants.unfiltered.xlsx'}[Filtering]
	json.dump(Unit, open(UnitsFile, 'wt'), indent = 4, ensure_ascii = False)
	
	StageAlias = 'Annovar'
	if StageAlias not in Unit['Stage']:
		ANNOVAR(
			InputVCF = os.path.join(Unit['OutputDir'], Unit["Output"]["VCF"]),
			OutputTSV = os.path.join(Unit['OutputDir'], Unit['Output']['AnnovarTable']),
			Databases = Unit["AnnovarDatabases"],
			DBFolder = Unit["AnnovarDatabasesPath"],
			AnnovarFolder = Unit["AnnovarFolder"],
			GenomeAssembly = Unit['Reference']['GenomeInfo']['annovar_alias'],
			Threads = Unit["Config"]["Threads"])
		Unit['Stage'].append(StageAlias)
		json.dump(Unit, open(UnitsFile, 'wt'), indent = 4, ensure_ascii = False)
	
	if Unit["GFF3"]:
		StageAlias = 'GFF3'
		if StageAlias not in Unit['Stage']:
			CureBase(
				DBDir = os.path.dirname(os.path.abspath(__file__)),
				InputVCF = os.path.join(Unit['OutputDir'], Unit["Output"]["VCF"]),
				OutputTSV = os.path.join(Unit['OutputDir'], Unit['Output']['AnnovarTable']),
				Databases = Unit["GFF3"],
				AnnovarFolder = Unit["AnnovarFolder"],
				Reference = os.path.join(Unit['Reference']['GenomeDir'], Unit['Reference']['GenomeInfo']['fasta']),
				GenomeAssembly = Unit['Reference']['GenomeInfo']['annovar_alias'],
				Threads = Unit["Config"]["Threads"])
			Unit['Stage'].append(StageAlias)
			json.dump(Unit, open(UnitsFile, 'wt'), indent = 4, ensure_ascii = False)
	
	StageAlias = 'Annofit'
	if StageAlias not in Unit['Stage']:
		AnnoFit(
			InputTSV = os.path.join(Unit['OutputDir'], Unit['Output']['AnnovarTable']),
			OutputXLSX = os.path.join(Unit['OutputDir'], Unit['Output']['VariantsTable']),
			HGMD = Unit["HGMDPath"],
			AnnovarFolder = Unit["AnnovarFolder"],
			AnnoFitConfig = Unit["AnnoFit"],
			ChunkSize = Unit["AnnoFit"]["AnnofitChunkSize"],
			Threads = Unit["Config"]["Threads"],
			Filtering = Filtering)
		Unit['Stage'].append(StageAlias)
		json.dump(Unit, open(UnitsFile, 'wt'), indent = 4, ensure_ascii = False)

def CreateParser():
	Parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=f"Scissors: Pipeline for Exome Sequence Analysis", epilog=f"Email: regnveig@ya.ru")
	Parser.add_argument('--version', action='version', version=__version__)

	Parser.add_argument('-a', '--annovar', required=True, help=f"Annovar folder")
	Parser.add_argument('-g', '--genome', required=True, help=f"Annovar genome alias")
	Parser.add_argument('-u', '--units', required=True, help=f"Units File in JSON format")
	Parser.add_argument('-n', '--nofilter', action='store_true', help=f"Don't filter variants")
	
	return Parser

def main():
	Parser = CreateParser()
	Namespace = Parser.parse_args(sys.argv[1:])
	AnnoPipe(Namespace.annovar, Namespace.units, "no" if Namespace.nofilter else "full", Namespace.genome)
