# +
from theforce.regression.gppotential import PosteriorPotential, PosteriorPotentialFromFolder
from theforce.calculator.posterior import AutoForceCalculator
from theforce.descriptor.atoms import AtomsData, LocalsData, TorchAtoms
from theforce.util.util import date
import torch
import ase
from ase.calculators.singlepoint import SinglePointCalculator
import numpy as np
import types
import warnings


def initial_model(gp, atoms, ediff):
    i = atoms.first_of_each_atom_type()
    inducing = LocalsData([atoms.loc[j] for j in i])
    data = AtomsData([atoms])
    model = PosteriorPotential(gp, data, inducing, use_caching=True)
    for j in range(atoms.natoms):
        if j not in i:
            model.add_1inducing(atoms.loc[j], ediff)
    return model


class Leapfrog:

    def __init__(self, dyn, gp, cutoff, ediff=0.1, fdiff=float('inf'), calculator=None, model=None,
                 algorithm='fast', volatile=None, logfile='leapfrog.log', skip=10, skip_volatile=3):
        self.dyn = dyn
        self.gp = gp
        self.cutoff = cutoff
        self.ediff = ediff
        self.fdiff = fdiff
        self.skip = skip
        self.skip_volatile = skip_volatile

        if type(algorithm) == str:
            self.algorithm = getattr(self, 'algorithm_'+algorithm)
        else:
            self.algorithm = types.MethodType(algorithm, self)

        # atoms
        if type(dyn.atoms) == ase.Atoms:
            self.to_ase = True
            dyn.atoms = TorchAtoms(dyn.atoms)
        else:
            self.to_ase = False
        self.atoms.update(cutoff=cutoff, descriptors=self.gp.kern.kernels)

        # calc
        if calculator:
            self.calculator = calculator
        else:
            self.calculator = dyn.atoms.calc

        # volatile
        self._volatile = volatile if volatile else 2 if model is None else -1

        # initiate
        self.step = 0
        self._fp = []
        self._fp_e = []
        self._ext = []
        self.logfile = logfile
        self.log('leapfrog says Hello!', mode='w')
        self.log('volatile: {}'.format(self._volatile))

        # model
        if model:
            if type(model) == str:
                potential = PosteriorPotentialFromFolder(model)
            else:
                potential = model
            self.log('a model is provided with {} data and {} ref(s)'.format(
                len(potential.data), len(potential.X)))
        else:
            snap = self.snapshot()
            potential = initial_model(self.gp, snap, self.ediff)
            potential._cutoff = cutoff
            self.log('update: {}  data: {}  inducing: {}  FP: {}'.format(
                True, len(potential.data), len(potential.inducing), len(self._fp)))
            self.log('a model is initiated with {} data and {} ref(s)'.format(
                len(potential.data), len(potential.X)))
        self.atoms.set_calculator(AutoForceCalculator(potential))
        self.energy = [self.atoms.get_potential_energy()]
        self.temperature = [self.atoms.get_temperature()]

    def log(self, mssge, mode='a'):
        with open(self.logfile, mode) as f:
            f.write('{} {} {}\n'.format(date(), self.step, mssge))

    @property
    def atoms(self):
        return self.dyn.atoms

    @atoms.setter
    def atoms(self, value):
        self.dyn.atoms = value

    @property
    def model(self):
        return self.atoms.calc.potential

    @property
    def sizes(self):
        return len(self.model.data), len(self.model.X)

    @property
    def fp_nodes(self):
        return self._fp, self._fp_e

    @property
    def ext_nodes(self):
        return self._ext, [self.energy[k] for k in self._ext]

    def volatile(self):
        return len(self._ext) <= self._volatile

    def rescale_velocities(self, factor):
        self.atoms.set_velocities(self.atoms.get_velocities()*factor)

    def strain_atoms(self, strain):
        warnings.warn('Leapfrog.strain_atoms is not robust!')
        cell = (np.eye(3) + strain) @ self.atoms.cell.T
        self.atoms.set_cell(cell, scale_atoms=True)

    def rescale_cell(self, f):
        self.atoms.set_cell(f*self.atoms.cell, scale_atoms=True)

    def snapshot(self, fake=False, copy=None):
        if copy is None:
            copy = self.atoms.copy()
        if fake:
            energy = self.atoms.get_potential_energy()
            forces = self.atoms.get_forces()
        else:
            if self.to_ase:
                tmp = copy.as_ase()
            else:
                tmp = copy
            tmp.set_calculator(self.calculator)
            energy = tmp.get_potential_energy()
            forces = tmp.get_forces()
            ase.io.Trajectory('_FP.traj', 'a').write(tmp)
            self._fp.append(self.step)
            self._fp_e.append(energy)
            self.log('exact energy: {}'.format(energy))
        copy.set_calculator(SinglePointCalculator(copy, energy=energy,
                                                  forces=forces))
        copy.set_targets()
        return copy

    def algorithm_robust(self, datafirst=True):
        new = self.snapshot()
        if datafirst is None:
            datafirst = np.random.choice([True, False])
        if datafirst:
            self.model.add_1atoms(new, self.ediff, self.fdiff)
        for loc in new.loc:
            ediff = self.ediff if self.sizes[1] > 1 else torch.finfo().tiny
            self.model.add_1inducing(loc, ediff)
        if not datafirst:
            self.model.add_1atoms(new, self.ediff, self.fdiff)

    def algorithm_fast(self):
        added_refs = 0
        for loc in self.atoms.calc.atoms:
            ediff = self.ediff if self.sizes[1] > 1 else torch.finfo().tiny
            change = self.model.add_1inducing(loc, ediff)
            if change >= ediff:
                added_refs += 1
        if added_refs > 0:
            new = self.snapshot()
            self.model.add_1atoms(new, self.ediff, self.fdiff)

    def algorithm_fastfast(self):
        locs = self.atoms.calc.atoms.loc
        leaks = self.model.leakages(locs)
        q = torch.argsort(leaks, descending=True)
        added_refs = 0
        for k in q:
            loc = locs[k]
            ediff = self.ediff if self.sizes[1] > 1 else torch.finfo().tiny
            change = self.model.add_1inducing(loc, ediff)
            if change >= ediff:
                added_refs += 1
            else:
                self.log('added refs: {}  ediff at break: {}'.format(
                    added_refs, change))
                break
        if added_refs > 0:
            new = self.snapshot()
            self.model.add_1atoms(new, self.ediff, self.fdiff)

    def algorithm_ultrafast(self):
        locs = self.atoms.calc.atoms.loc
        leaks = self.model.leakages(locs)
        q = torch.argsort(leaks, descending=True)
        added_refs = 0
        for k in q:
            loc = locs[k]
            ediff = self.ediff if self.sizes[1] > 1 else torch.finfo().tiny
            change = self.model.add_1inducing(loc, ediff)
            if change >= ediff:
                added_refs += 1
            else:
                self.log('added refs: {}  ediff at break: {}'.format(
                    added_refs, change))
                break
        if added_refs > 0:
            a = len(self.model.data)
            new = self.snapshot(fake=True)
            de, df = self.model.add_1atoms(new, self.ediff, self.fdiff)
            if len(self.model.data) > a:
                self.model.pop_1data(clear_cached=True)
                new = self.snapshot(copy=new)
                self.model.add_1atoms(new, self.ediff, self.fdiff)

    def update_model(self):
        size1 = self.sizes
        if self.volatile():
            self.algorithm_robust()
        else:
            self.algorithm()
        size2 = self.sizes
        self.data_plus = size2[0]-size1[0]
        self.ref_plus = size2[1]-size1[1]
        tf = self.data_plus > 0 or self.ref_plus > 0
        if tf:
            self.atoms.calc.results.clear()
        return tf

    def undo_update(self):
        d = self.data_plus
        i = self.ref_plus
        while d > 0:
            self.model.pop_1data()
            d -= 1
        while i > 0:
            self.model.pop_1inducing()
            i -= 1

    def doit(self, prob=1):

        # check
        ext = False
        if len(self.energy) >= 3:
            d1 = self.energy[-1] - self.energy[-2]
            d2 = self.energy[-2] - self.energy[-3]
            if d1*d2 < 0:
                ext = True
                # unless it's a artificial ext!
                if len(self._ext) > 0 and self.step - self._ext[-1] == 1:
                    ext = False

        # decide
        last = 0 if len(self._fp) == 0 else self._fp[-1]
        if ext:
            self.log('extremum')
            self._ext += [self.step]
            if not self.volatile() and self._ext[-1]-last < self.skip:
                return False
            return np.random.choice([True, False], p=[prob, 1-prob])  # main
        else:
            if self.volatile() and ((self.step == 0 and len(self._fp) == 0)
                                    or self.step-last > self.skip_volatile):
                return True
            return False  # main

    def run(self, maxsteps, prob=1):
        for _ in range(maxsteps):
            if prob > 0 and self.doit(prob=prob):
                self.log('updating ...')
                self.log('update: {}  data: {}  inducing: {}  FP: {}'.format(
                    self.update_model(), *self.sizes, len(self._fp)))
            self.dyn.run(1)
            self.step += 1
            self.energy += [self.atoms.get_potential_energy()]
            self.temperature += [self.atoms.get_temperature()]
            self.log('{} {}'.format(self.energy[-1], self.temperature[-1]))

    def run_updates(self, maxupdates, prob=1):
        updates = 0
        steps = 0
        stresses = []
        while updates < maxupdates:
            if prob > 0 and self.doit(prob=prob):
                self.log('updating ...')
                self.log('update: {}  data: {}  inducing: {}  FP: {}'.format(
                    self.update_model(), *self.sizes, len(self._fp)))
                updates += 1
            self.dyn.run(1)
            self.step += 1
            steps += 1
            self.energy += [self.atoms.get_potential_energy()]
            stresses += [self.atoms.get_stress().reshape(1, -1)]
            self.temperature += [self.atoms.get_temperature()]
            self.log('{} {}'.format(self.energy[-1], self.temperature[-1]))
        steps_per_update = steps / updates
        average_energy = np.array(self.energy[-steps:]).mean()
        average_temp = np.array(self.temperature[-steps:]).mean()
        self.log('steps per update: {}, energy: {}, temperature: {}'.format(
            steps_per_update, average_energy, average_temp))
        stress = np.concatenate(stresses).mean(axis=0)
        self.log('stress: {}'.format(stress))
        return steps_per_update, average_energy, average_temp, stress
